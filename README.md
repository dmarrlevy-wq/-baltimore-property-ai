# Baltimore Property AI

Runnable Streamlit MVP for Baltimore City public-record property intelligence.

## Run
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

The application pulls public Baltimore ArcGIS services at runtime, combines tax-sale candidates with Baltimore property information and DHCD housing signals, and produces an explainable 0–100 signal score.

Included:
- 2026 Baltimore tax-sale connector
- Baltimore property information connector
- DHCD vacant-building connector
- DHCD foreclosure-filings connector
- configurable 311 connector
- absentee-owner heuristic
- ownership-duration calculation
- assessed value / sales / physical property fields
- ranked lead dashboard
- filters
- CSV export
- property intelligence view
- 311 lookup
- deal analyzer / MAO calculator
- map support when geographic points are returned
