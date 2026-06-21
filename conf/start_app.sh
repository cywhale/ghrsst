#!/bin/bash

# TLS should terminate at the reverse proxy. Keep the local upstream on plain HTTP.
/home/odbadmin/.pyenv/versions/py314/bin/gunicorn ghrsst_app:app -w 2 -k uvicorn.workers.UvicornWorker -b 127.0.0.1:8035 --timeout 180
