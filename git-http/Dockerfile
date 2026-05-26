FROM alpine:3.20

# git provides git-http-backend at /usr/libexec/git-core/git-http-backend.
# fcgiwrap runs the CGI; nginx terminates HTTP and proxies to it via FastCGI.
RUN apk add --no-cache git nginx fcgiwrap \
    # Bind-mounted rules dir comes from a different host UID; trust it so
    # git won't reject the working tree as "dubious ownership". Read-only
    # mount means this can't be abused to write.
    && git config --system --add safe.directory '*' \
    && mkdir -p /run/nginx /run/fcgiwrap

COPY nginx.conf /etc/nginx/nginx.conf
COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 8080
ENTRYPOINT ["/start.sh"]
