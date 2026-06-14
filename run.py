#!/usr/bin/env python3
"""
Run the GRControlSoftware backend.
Open http://localhost:8000 in your browser after starting.
"""
import sys
import asyncio
import uvicorn

# pyserial-asyncio requires SelectorEventLoop on Windows
# (Python 3.8+ defaults to ProactorEventLoop which is incompatible)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_dirs=["backend"],
    )
