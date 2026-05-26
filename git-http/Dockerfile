FROM alpine:3.20

# git provides git-http-backend at /usr/libexec/git-core/git-http-backend.
# fcgiwrap runs the CGI; nginx terminates HTTP and proxies to it via FastCGI.
RUN apk add --no-cache git nginx fcgiwrap \
    # Bind-mounted rules dir comes from a different host UID; trust it so
    # git won't reject the working tree as "dubious ownership". Read-only
    # mount means this can't be abused to write.
    && git config --system --add safe.directory '*'

COPY nginx.conf /etc/nginx/nginx.conf
COPY start.sh /start.sh
RUN chmod +x /start.sh

# Run as the unprivileged nginx user so cap_drop: ALL doesn't trip on
# /var/lib/nginx (nginx-owned) or /run/nginx (nginx-owned). Listening on
# 8080 means we don't need NET_BIND_SERVICE.
USER nginx
EXPOSE 8080
ENTRYPOINT ["/start.sh"]
