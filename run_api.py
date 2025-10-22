"""
Small wrapper to run the FastAPI app in `backend.api` with uvicorn.

Usage:
    python3 run_api.py      # runs uvicorn on 0.0.0.0:8080

You can also run directly with uvicorn:
    uvicorn backend.api:app --host 0.0.0.0 --port 8080 --reload

"""
import os
import sys
import uvicorn

if __name__ == '__main__':
    host = os.environ.get('API_HOST', '0.0.0.0')
    port = int(os.environ.get('API_PORT', '8080'))
    reload = os.environ.get('API_RELOAD', 'true').lower() in ('1', 'true', 'yes')
    # Ensure the project root is on sys.path so imports like `backend.*` work
    root = os.path.dirname(os.path.abspath(__file__))
    if root not in sys.path:
        sys.path.insert(0, root)

    uvicorn.run('backend.api:app', 
                host=host,
                port=port,
                reload=reload,
                # ssl_keyfile="key.pem",  # Path to your private key
                # ssl_certfile="cert.pem"  # Path to your certificate
                )
