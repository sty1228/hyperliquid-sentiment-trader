
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn, os, io

ROOT = os.path.dirname(os.path.dirname(__file__)) 
INDEX = os.path.join(ROOT, "web", "index.html")   

app = FastAPI(title="Static Host for Dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

def _read_index():
    with io.open(INDEX, "r", encoding="utf-8") as f:
        return f.read()

@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
def home():
    return HTMLResponse(_read_index(), media_type="text/html; charset=utf-8")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8088)
