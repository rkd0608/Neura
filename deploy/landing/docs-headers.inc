# The docs (/docs/): the site's headers, with a script policy that allows the inline scripts
# Docusaurus writes by their hashes (docs-csp.inc, made from the build by site/scripts/csp.mjs).
include /etc/nginx/conf.d/base-headers.inc;
include /etc/nginx/conf.d/docs-csp.inc;
