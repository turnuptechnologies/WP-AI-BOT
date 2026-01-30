from fastapi import FastAPI
from routers import user, bot

app = FastAPI(title="MWI AI API")

app.include_router(user.router)
app.include_router(bot.router)

@app.get("/")
def root():
    return {"status": "running"}
