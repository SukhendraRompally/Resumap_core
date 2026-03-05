import os
from dotenv import load_dotenv

# This tells Python to find the .env file in the current folder 
# and manually inject the variables into the script.
load_dotenv() 

REPLIT = os.getenv("REPLIT_BASE_URL")

from fastapi import FastAPI, BackgroundTasks, Header, HTTPException
from pydantic import BaseModel
import scout
import uvicorn

app = FastAPI()
API_SECRET = os.getenv("TAILOR_SECRET")
class ReplitPayload(BaseModel):
    user_id: str
    resume_text: str
    user_profile: dict

@app.post("/tailor-text")
async def handle_replit_request(
    data: dict, 
    background_tasks: BackgroundTasks,
    x_tailor_secret: str = Header(None) # Header is now imported correctly
):
    # 2. Security Check
    if x_tailor_secret != API_SECRET:
        # HTTPException is now imported correctly
        raise HTTPException(status_code=403, detail="Unauthorized")

    # 3. Data Extraction
    # We use .get() to avoid crashing if Replit sends a partial object
    resume_text = data.get("extracted_text")
    user_profile = data.get("user_profile")
    user_id = data.get("user_id")

    if not all([resume_text, user_profile, user_id]):
        raise HTTPException(status_code=422, detail="Missing required fields: extracted_text, user_profile, or user_id")

    # 4. Hand off to scout.py
    # This runs the long-running job in the background so Replit doesn't wait
    background_tasks.add_task(
        scout.run_automation_pipeline, 
        resume_text, 
        user_profile, 
        user_id,
        None  # final_rankings - None when called from API
    )

    return {"status": "success", "message": f"Pipeline triggered for User {user_id}"}

if __name__ == "__main__":
    # Host 0.0.0.0 makes the VM listen to external requests (like Replit)
    uvicorn.run(app, host="0.0.0.0", port=8000)