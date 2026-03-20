import asyncio
import os
from stagehand import AsyncStagehand

async def check():
    # Use the key from your environment variable
    api_key = os.getenv("MODEL_API_KEY")
    
    # We pass local_openai_api_key to satisfy Stagehand's internal server requirements
    client = AsyncStagehand(
        model_api_key=api_key, 
        local_openai_api_key=api_key
    )
    
    print("🚀 Forcing local session to probe methods...")
    
    try:
        # We MUST specify browser type as local to avoid the x-bb-api-key error
        session = await client.sessions.start(
            model_name="gpt-4o",
            browser={"type": "local"} 
        )
        
        print("\n--- 🔍 INSPECTING SESSION OBJECT ---")
        methods = dir(session)
        
        # Checking the core suspects
        results = {
            "page": "page" in methods,
            "screenshot": "screenshot" in methods,
            "browser": "browser" in methods
        }

        for key, found in results.items():
            print(f"Does it have '.{key}'? {'✅ YES' if found else '❌ NO'}")
        
        print("\nAvailable public methods:")
        print([m for m in methods if not m.startswith("_")])
        
        await session.end()
    except Exception as e:
        print(f"❌ Connection failed: {e}")

if __name__ == "__main__":
    asyncio.run(check())