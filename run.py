#!/usr/bin/env python3
"""
Run the GRControlSoftware backend.
Open http://localhost:8000 in your browser after starting.
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_dirs=["backend"],
    )
