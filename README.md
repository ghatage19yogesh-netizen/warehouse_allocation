# Allocation Planner (Streamlit demo)

Decides how many units of each product each warehouse should stock, at the lowest
shipping cost, using sample data. Simplified version of the COM allocation engine
(rule-based placements + linear program; SciPy HiGHS instead of Gurobi).

## Run locally
    pip install -r requirements.txt
    streamlit run app.py

## Share it (Streamlit Community Cloud, free)
1. Create a new GitHub repo and upload these files (keep the `.streamlit` folder).
2. Go to https://share.streamlit.io, sign in with GitHub, click **Create app**.
3. Pick the repo, branch `main`, main file `app.py`, then **Deploy**.
4. Copy the app link (https://<name>.streamlit.app) and share it.

## Files
- `app.py` – the 3-screen app (Set up & run, See the plan, Check warnings)
- `engine.py` – sample data and the allocation logic
- `test_app.py` – automated check: `python test_app.py`
