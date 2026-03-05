import os
from openai import AzureOpenAI
from dotenv import load_dotenv

# Load the vault
load_dotenv()

# Setup the connection
client = AzureOpenAI(
    api_key=os.getenv("AZURE_OPENAI_KEY"),  
    api_version=os.getenv("AZURE_OPENAI_VERSION"),
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT")
)

try:
    response = client.chat.completions.create(
        model=os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME"),
        messages=[{"role": "user", "content": "Say hello!"}]
    )
    print("\n✅ SUCCESS! Azure responded with:")
    print(response.choices[0].message.content)
except Exception as e:
    print("\n❌ FAILED!")
    print(f"Error: {e}")
