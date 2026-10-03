
import re, math, time
from datetime import datetime
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st

try:
    import folium
    from streamlit_folium import st_folium
    HAS_MAP = True
except Exception:
    HAS_MAP = False

st.set_page_config(page_title="Baltimore Property AI", page_icon="🏠", layout="wide")

# -------------------- Public Baltimore ArcGIS sources --------------------
PROPERTY_URL = "https://geodata.baltimorecity.gov/egis/rest/services/CityView/Realproperty_OB/FeatureServer/0"
TAXSALE_URL = "https://egis.baltimorecity.gov/egis/rest/services/Housing/Tax_Sale_2026/FeatureServer/0"
DHCD_URL = "https://baltegis.baltimorecity.gov/mapping/rest/services/Housing/DHCD_Open_Baltimore_Datasets/FeatureServer"
VACANCY_URL = DHCD_URL + "/1"
FORECLOSURE_URL = DHCD_URL + "/11"
# Baltimore publishes multiple 311 services. This connector can be overridden in the sidebar.
DEFAULT_311_URL = "https://gisdata.baltimorecity.gov/egis/rest/services/311/311_Open_Traffic_cases/FeatureServer/0"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "BaltimorePropertyAI/1.0"})

def arcgis_json(url, params=None, timeout=25):
    p = {"f": "json"}
    if params:
        p.update(params)
    r = SESSION.get(url, params=p, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message", "ArcGIS error"))
    return data

@st.cache_data(ttl=900, show_spinner=False)
def layer_meta(url):
    return arcgis_json(url)

@st.cache_data(ttl=900, show_spinner=False)
def arcgis_query(url, where="1=1", out_fields="*", result_record_count=2000,
                 return_geometry=False, order_by=None):
    params = {
        "where": where,
        "outFields": out_fields,
        "returnGeometry": "true" if return_geometry else "false",
        "resultRecordCount": result_record_count,
        "f": "json",
    }
    if order_by:
        params["orderByFields"] = order_by
    data = arcgis_json(url, params)
    rows = []
    for f in data.get("features", []):
        a = f.get("attributes", {})
        if return_geometry:
            g = f.get("geometry")
            a["_geometry"] = g
        rows.append(a)
    return pd.DataFrame(rows)

def pick_field(df, names):
    cols = {str(c).upper(): c for c in df.columns}
    for n in names:
        if n.upper() in cols:
            return cols[n.upper()]
    return None

def norm_addr(x):
    if pd.isna(x): return ""
    x = str(x).upper()
    x = re.sub(r"[^A-Z0-9 ]", " ", x)
    x = re.sub(r"\s+", " ", x).strip()
    return x

def num(x):
    try:
        if pd.isna(x): return 0.0
        return float(str(x).replace("$","").replace(",",""))
    except Exception:
        return 0.0

def score_row(r):
    score = 0.0
    reasons = []
    # Signals are intentionally transparent, not a prediction of profitability.
    lien = num(r.get("LIEN_AMOUNT", 0))
    if lien > 0:
        pts = min(24, 8 + math.log10(max(lien,1))*5)
        score += pts; reasons.append(f"Tax-sale lien ${lien:,.0f}")
    if bool(r.get("TAX_SALE", False)):
        score += 14; reasons.append("2026 tax-sale record")
    if bool(r.get("VACANT", False)):
        score += 18; reasons.append("Open vacant-building signal")
    if bool(r.get("FORECLOSURE", False)):
        score += 18; reasons.append("Foreclosure filing signal")
    complaints = int(num(r.get("311_COUNT",0)))
    if complaints:
        pts = min(12, 2 + complaints * 0.75)
        score += pts; reasons.append(f"{complaints} matched 311 record(s)")
    if bool(r.get("ABSENTEE", False)):
        score += 7; reasons.append("Mailing address differs from property address")
    years = num(r.get("OWNERSHIP_YEARS",0))
    if years >= 15:
        score += 5; reasons.append(f"{years:.0f} years since recorded sale")
    elif years >= 10:
        score += 3; reasons.append(f"{years:.0f} years since recorded sale")
    year = num(r.get("YEAR_BUILD",0))
    if 0 < year < 1950:
        score += 4; reasons.append("Older building")
    value = num(r.get("ASSESSED_TOTAL",0))
    if value > 0 and lien > 0:
        ratio = lien / value
        if ratio >= .10:
            score += 4; reasons.append("Lien is material relative to assessed value")
        elif ratio >= .05:
            score += 2; reasons.append("Lien is notable relative to assessed value")
    return round(min(100, score),1), reasons

def merge_properties(base, extra, key="BLOCKLOT"):
    if base.empty or extra.empty or key not in base or key not in extra:
        return base
    e = extra.drop_duplicates(key)
    return base.merge(e, on=key, how="left", suffixes=("","_src"))

def get_blocklot(df):
    return pick_field(df, ["BLOCKLOT","BLOCK_LOT","BLOCKLOTNO"])

def get_address(df):
    return pick_field(df, ["FULLADDR","ADDRESS","SITEADDRESS","SITUS","LOCATION"])

def get_owner(df):
    return pick_field(df, ["OWNER_1","OWNER","OWNERNAME","NAME"])

def load_candidates(limit=2000):
    tax = arcgis_query(TAXSALE_URL, result_record_count=limit, return_geometry=True)
    if tax.empty:
        return tax, pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    b = get_blocklot(tax)
    addr = get_address(tax)
    owner = get_owner(tax)
    lien = pick_field(tax, ["LIEN_AMOUNT","LIENAMOUNT","LIEN","AMOUNT"])
    if b:
        tax["BLOCKLOT"] = tax[b].astype(str)
    if addr:
        tax["ADDRESS"] = tax[addr]
    if owner:
        tax["OWNER"] = tax[owner]
    if lien:
        tax["LIEN_AMOUNT"] = tax[lien]
    else:
        tax["LIEN_AMOUNT"] = 0

    props = arcgis_query(PROPERTY_URL, result_record_count=2000, return_geometry=True)
    pb = get_blocklot(props)
    if pb:
        props["BLOCKLOT"] = props[pb].astype(str)

    # Normalize core property fields
    mapping = {
        "OWNER_1":"OWNER_PROPERTY","MAILTOADD":"MAILING_ADDRESS",
        "FULLADDR":"PROPERTY_ADDRESS","NEIGHBOR":"NEIGHBORHOOD",
        "BFCVLAND":"ASSESSED_LAND","BFCVIMPR":"ASSESSED_IMPROVEMENTS",
        "FULLCASH":"ASSESSED_TOTAL","SALEDATE":"SALE_DATE",
        "SALEPRIC":"SALE_PRICE","YEAR_BUILD":"YEAR_BUILD",
        "STRUCTAREA":"STRUCTURE_SQFT","ZONECODE":"ZONING","VACIND":"VACANT_INDICATOR",
        "PIN":"PIN","SDATCODE":"LAND_USE_CODE"
    }
    for src, dst in mapping.items():
        if src in props.columns:
            props[dst] = props[src]

    for c in ["ASSESSED_LAND","ASSESSED_IMPROVEMENTS","ASSESSED_TOTAL","SALE_PRICE","YEAR_BUILD","STRUCTURE_SQFT"]:
        if c in props: props[c] = pd.to_numeric(props[c], errors="coerce").fillna(0)

    out = tax.copy()
    if "BLOCKLOT" in out.columns and "BLOCKLOT" in props.columns:
        keep = [c for c in ["BLOCKLOT","PROPERTY_ADDRESS","OWNER_PROPERTY","MAILING_ADDRESS","NEIGHBORHOOD",
                            "ASSESSED_LAND","ASSESSED_IMPROVEMENTS","ASSESSED_TOTAL","SALE_DATE","SALE_PRICE",
                            "YEAR_BUILD","STRUCTURE_SQFT","ZONING","VACANT_INDICATOR","PIN","LAND_USE_CODE","_geometry"] if c in props]
        out = out.merge(props[keep].drop_duplicates("BLOCKLOT"), on="BLOCKLOT", how="left")
    return out, props, tax, pd.DataFrame()

def load_signals(df, use_vacancy=True, use_foreclosure=True):
    out = df.copy()
    if "BLOCKLOT" not in out:
        out["BLOCKLOT"] = ""
    if use_vacancy:
        try:
            v = arcgis_query(VACANCY_URL, result_record_count=2000)
            vb = get_blocklot(v)
            if vb:
                v["BLOCKLOT"] = v[vb].astype(str)
                vc = "BLOCKLOT"
                v["_VAC"] = True
                out = out.merge(v[[vc,"_VAC"]].drop_duplicates(vc), on="BLOCKLOT", how="left")
                out["VACANT"] = out["_VAC"].fillna(False)
            else:
                out["VACANT"] = False
        except Exception:
            out["VACANT"] = False
    else:
        out["VACANT"] = False

    if use_foreclosure:
        try:
            f = arcgis_query(FORECLOSURE_URL, result_record_count=2000)
            fb = get_blocklot(f)
            if fb:
                f["BLOCKLOT"] = f[fb].astype(str)
                f["_FC"] = True
                out = out.merge(f[["BLOCKLOT","_FC"]].drop_duplicates("BLOCKLOT"), on="BLOCKLOT", how="left")
                out["FORECLOSURE"] = out["_FC"].fillna(False)
            else:
                out["FORECLOSURE"] = False
        except Exception:
            out["FORECLOSURE"] = False
    else:
        out["FORECLOSURE"] = False

    # Ownership duration / absentee heuristic
    sale = pd.to_datetime(out.get("SALE_DATE"), errors="coerce")
    out["OWNERSHIP_YEARS"] = ((pd.Timestamp.today() - sale).dt.days / 365.25).clip(lower=0)
    pa = out.get("PROPERTY_ADDRESS", out.get("ADDRESS","")).map(norm_addr) if "PROPERTY_ADDRESS" in out else out.get("ADDRESS","").map(norm_addr)
    ma = out.get("MAILING_ADDRESS","").map(norm_addr) if "MAILING_ADDRESS" in out else pd.Series("", index=out.index)
    out["ABSENTEE"] = (pa != "") & (ma != "") & ~ma.str.contains(pa.str[:12], regex=False, na=False)
    return out

@st.cache_data(ttl=900, show_spinner=False)
def load_all(limit):
    return load_candidates(limit)

def query_311(address, service_url):
    if not address:
        return pd.DataFrame()
    meta = layer_meta(service_url)
    fields = [f["name"] for f in meta.get("fields",[])]
    # Find likely address fields dynamically.
    candidates = [f for f in fields if any(k in f.upper() for k in ["ADDRESS","LOCATION","STREET"])]
    if not candidates:
        return pd.DataFrame()
    where_parts = []
    for f in candidates[:4]:
        safe = address.replace("'","''")
        where_parts.append(f"UPPER({f}) LIKE UPPER('%{safe}%')")
    where = " OR ".join(where_parts)
    return arcgis_query(service_url, where=where, result_record_count=1000)

# -------------------- UI --------------------
st.title("🏠 Baltimore Property AI")
st.caption("Public-record property intelligence • Baltimore City • Explainable lead scoring")

with st.sidebar:
    st.header("Data & Filters")
    limit = st.slider("Tax-sale records to load", 100, 2000, 1000, 100)
    min_score = st.slider("Minimum score", 0, 100, 0)
    min_lien = st.number_input("Minimum lien ($)", 0.0, 1_000_000.0, 0.0, 500.0)
    vacant_only = st.checkbox("Vacant-building signal only")
    foreclosure_only = st.checkbox("Foreclosure signal only")
    absentee_only = st.checkbox("Absentee-owner indicator only")
    st.divider()
    st.caption("Public connectors")
    st.code("Baltimore ArcGIS / DHCD / 311")
    st.caption("311 endpoint can be changed below if Baltimore publishes a replacement service.")
    service_311 = st.text_input("311 FeatureServer URL", DEFAULT_311_URL)

try:
    data, props, taxraw, _ = load_all(limit)
    data = load_signals(data)
except Exception as e:
    st.error(f"Data source error: {e}")
    st.stop()

if data.empty:
    st.warning("No tax-sale records were returned by the current public service.")
    st.stop()

data["SCORE"], data["WHY"] = zip(*data.apply(score_row, axis=1))
data["ADDRESS_DISPLAY"] = data.get("PROPERTY_ADDRESS", data.get("ADDRESS","")).fillna("")
data["OWNER_DISPLAY"] = data.get("OWNER_PROPERTY", data.get("OWNER","")).fillna("")
data["ASSESSED_TOTAL"] = pd.to_numeric(data.get("ASSESSED_TOTAL",0), errors="coerce").fillna(0)
data["LIEN_AMOUNT"] = pd.to_numeric(data.get("LIEN_AMOUNT",0), errors="coerce").fillna(0)

filtered = data[
    (data.SCORE >= min_score) &
    (data.LIEN_AMOUNT >= min_lien) &
    ((~vacant_only) | data.VACANT) &
    ((~foreclosure_only) | data.FORECLOSURE) &
    ((~absentee_only) | data.ABSENTEE)
].sort_values(["SCORE","LIEN_AMOUNT"], ascending=[False,False]).reset_index(drop=True)

c1,c2,c3,c4 = st.columns(4)
c1.metric("Candidates", len(data))
c2.metric("Showing", len(filtered))
c3.metric("Vacant signals", int(data.VACANT.sum()))
c4.metric("Foreclosure signals", int(data.FORECLOSURE.sum()))

tab1, tab2, tab3, tab4 = st.tabs(["📊 Ranked Leads","🗺️ Map","🔍 Property Intelligence","🧮 Deal Analyzer"])

with tab1:
    show_cols = [c for c in ["SCORE","ADDRESS_DISPLAY","OWNER_DISPLAY","LIEN_AMOUNT","VACANT","FORECLOSURE",
                             "ABSENTEE","OWNERSHIP_YEARS","ASSESSED_TOTAL","YEAR_BUILD","NEIGHBORHOOD"] if c in filtered]
    display = filtered[show_cols].copy()
    display.rename(columns={"SCORE":"Score","ADDRESS_DISPLAY":"Address","OWNER_DISPLAY":"Owner",
                            "LIEN_AMOUNT":"Tax-sale lien","VACANT":"Vacant","FORECLOSURE":"Foreclosure",
                            "ABSENTEE":"Absentee","OWNERSHIP_YEARS":"Ownership years",
                            "ASSESSED_TOTAL":"Assessed total","YEAR_BUILD":"Year built","NEIGHBORHOOD":"Neighborhood"}, inplace=True)
    st.dataframe(display, use_container_width=True, height=520)
    csv = display.to_csv(index=False).encode()
    st.download_button("⬇️ Export ranked CSV", csv, "baltimore_property_ai_leads.csv", "text/csv")

with tab2:
    if not HAS_MAP:
        st.info("Install folium and streamlit-folium to enable the interactive map.")
    else:
        m = folium.Map(location=[39.2904,-76.6122], zoom_start=11, tiles="CartoDB positron")
        count = 0
        for _, r in filtered.head(500).iterrows():
            g = r.get("_geometry")
            if not isinstance(g, dict): continue
            # ArcGIS geometry is projected; use centroid only when x/y are already geographic.
            # Baltimore tax-sale service commonly returns StatePlane coordinates, so omit uncertain geometry.
            x, y = g.get("x"), g.get("y")
            if x is not None and y is not None and -180 <= x <= 180 and -90 <= y <= 90:
                folium.CircleMarker([y,x], radius=5, tooltip=f"{r.get('ADDRESS_DISPLAY','')} • {r.SCORE:.0f}").add_to(m)
                count += 1
        st_folium(m, width=None, height=600)
        st.caption(f"Mapped {count} records with geographic point coordinates available from the response.")

with tab3:
    if filtered.empty:
        st.info("No properties match the current filters.")
    else:
        choices = filtered["ADDRESS_DISPLAY"].fillna("").tolist()
        selected = st.selectbox("Select a property", choices)
        r = filtered[filtered["ADDRESS_DISPLAY"] == selected].iloc[0]
        a,b,c = st.columns(3)
        a.metric("Opportunity signal score", f"{r.SCORE:.0f}/100")
        b.metric("Tax-sale lien", f"${r.LIEN_AMOUNT:,.0f}")
        c.metric("Assessed total", f"${r.ASSESSED_TOTAL:,.0f}")
        st.subheader(selected or "Property")
        st.write(f"**Owner:** {r.get('OWNER_DISPLAY','')}")
        st.write(f"**Mailing address:** {r.get('MAILING_ADDRESS','')}")
        st.write(f"**Block/Lot:** {r.get('BLOCKLOT','')}")
        st.write(f"**Neighborhood:** {r.get('NEIGHBORHOOD','')}")
        st.write(f"**Year built:** {r.get('YEAR_BUILD','')}")
        st.write(f"**Structure area:** {r.get('STRUCTURE_SQFT','')}")
        st.write(f"**Zoning:** {r.get('ZONING','')}")
        st.write(f"**Last sale:** {r.get('SALE_DATE','')} — ${num(r.get('SALE_PRICE',0)):,.0f}")
        st.write(f"**Ownership duration:** {num(r.get('OWNERSHIP_YEARS',0)):.1f} years")
        st.write(f"**Vacant-building signal:** {'Yes' if r.get('VACANT') else 'No'}")
        st.write(f"**Foreclosure signal:** {'Yes' if r.get('FORECLOSURE') else 'No'}")
        st.write(f"**Absentee indicator:** {'Yes' if r.get('ABSENTEE') else 'No'}")
        st.subheader("Why this property ranked here")
        for reason in r.get("WHY", []):
            st.write("• " + reason)
        st.caption("The score measures documented public-record signals. It does not predict seller intent, legal outcome, or investment profitability.")

        if st.button("🔎 Query 311 for this address"):
            with st.spinner("Querying Baltimore 311 service..."):
                try:
                    results = query_311(selected, service_311)
                    st.metric("Matched 311 records", len(results))
                    if not results.empty:
                        st.dataframe(results, use_container_width=True)
                except Exception as e:
                    st.error(f"311 query failed: {e}")

with tab4:
    st.subheader("Deal Analyzer")
    st.caption("User-entered underwriting tool. It does not establish market value or guarantee a profit.")
    x1,x2 = st.columns(2)
    with x1:
        arv = st.number_input("Estimated ARV ($)", min_value=0.0, value=250000.0, step=5000.0)
        repairs = st.number_input("Repairs ($)", min_value=0.0, value=50000.0, step=5000.0)
        closing = st.number_input("Closing / selling costs ($)", min_value=0.0, value=15000.0, step=1000.0)
    with x2:
        profit = st.number_input("Target profit ($)", min_value=0.0, value=30000.0, step=5000.0)
        fixed = st.number_input("Other fixed / assignment costs ($)", min_value=0.0, value=5000.0, step=500.0)
        purchase = st.number_input("Proposed purchase price ($)", min_value=0.0, value=100000.0, step=5000.0)
    mao = max(0, arv - repairs - closing - profit - fixed)
    spread = arv - repairs - closing - fixed - purchase
    d1,d2,d3 = st.columns(3)
    d1.metric("Estimated MAO", f"${mao:,.0f}")
    d2.metric("Gross spread", f"${spread:,.0f}")
    d3.metric("Offer vs. MAO", f"${purchase-mao:,.0f}")
    if purchase <= mao and purchase > 0:
        st.success("Entered purchase price is at or below the user-defined MAO.")
    elif purchase > mao:
        st.warning("Entered purchase price is above the user-defined MAO.")

st.divider()
st.caption("Sources: Baltimore City ArcGIS property, tax-sale, DHCD housing, and public 311 services. Verify every record before relying on it.")
