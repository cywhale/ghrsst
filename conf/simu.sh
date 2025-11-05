# run API server
## localhost: /home/odbadmin/.pyenv/versions/py314/bin/gunicorn ghrsst_app:app -k uvicorn.workers.UvicornWorker -b 127.0.0.1:8035 --timeout 120
/home/odbadmin/.pyenv/versions/py314/bin/gunicorn ghrsst_app:app -w 2 -k uvicorn.workers.UvicornWorker -b 127.0.0.1:8035 --keyfile conf/privkey.pem --certfile conf/fullchain.pem --reload --timeout 120

# kill process
ps -ef | grep 'ghrsst_app' | grep -v grep | awk '{print $2}' | xargs -r kill -9

# pm2 start
pm2 start ./conf/ecosystem.config.js
