from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def read_root():
    return {"message": "Asimovwatch API v1 is alive"}
