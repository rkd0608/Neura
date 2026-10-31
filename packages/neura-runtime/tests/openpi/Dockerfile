# openpi's own policy server, unmodified, for the openpi transport's test (test_local.py): its
# websocket server and its client library at a pinned commit, serving a stand-in policy
# (serve_still.py, mounted by the test). Build it with
#   docker build -t neura-openpi-test:dev packages/neura-runtime/tests/openpi
# (behind a TLS-intercepting proxy, add --secret id=ca_bundle,src=/path/to/ca.pem).
FROM mirror.gcr.io/library/python@sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60
ARG OPENPI=215abfb217dbac7d5f1273282331b9b1866c0479
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -s /run/secrets/ca_bundle ]; then export PIP_CERT=/run/secrets/ca_bundle; fi; \
    pip install --no-cache-dir "openpi-client==0.1.2"
ADD https://raw.githubusercontent.com/Physical-Intelligence/openpi/${OPENPI}/src/openpi/serving/websocket_policy_server.py /opt/openpi/websocket_policy_server.py
ENV PYTHONPATH=/opt/openpi
