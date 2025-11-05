#!/bin/bash

/home/odbadmin/.pyenv/versions/py314/bin/gunicorn ghrsst_app:app -w 2 -k uvicorn.workers.UvicornWorker -b 127.0.0.1:8035 --keyfile conf/privkey.pem --certfile conf/fullchain.pem --timeout 180 --reload
