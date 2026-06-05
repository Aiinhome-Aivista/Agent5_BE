# File: backend/main.py

import os
import uvicorn

# Custom port from environment variable
PORT = int(os.getenv("PORT", 3004))

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=PORT,
        reload=True
    )