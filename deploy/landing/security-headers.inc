# Security headers for every response of the site (included by default.conf, and by each location
# that sets its own headers: nginx drops inherited add_header directives there).
include /etc/nginx/conf.d/base-headers.inc;
add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'" always;
