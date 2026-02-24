"""Run the PLGO Options web application."""

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "plgo_options.web.app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )