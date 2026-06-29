bind = "127.0.0.1:5000"
workers = 2
timeout = 30
accesslog = "/home/deploy/mini-gpay/logs/gunicorn-access.log"
errorlog = "/home/deploy/mini-gpay/logs/gunicorn-error.log"
loglevel = "info"
