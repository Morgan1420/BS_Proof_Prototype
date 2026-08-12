# BS_Proof_Prototype
A prototype for the BS Proff app

## Activate Backend
source venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

## Remove all entries from DB
source venv/bin/activate
python -c "
from sqlmodel import Session
from app.db import engine
from app.services import storage

with Session(engine) as session:
    result = storage.delete_all_data(session)
    print(f'Deleted {result[\"products\"]} product(s), {result[\"links\"]} link(s), {result[\"ingredients\"]} ingredient(s).')
"
