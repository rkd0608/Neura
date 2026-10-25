// Sign in (design §20.7): with the organization's SSO when the control plane publishes one, or
// with an API token.
import { useEffect, useState } from "react";
import { Navigate, useNavigate } from "react-router";

import { useAuth } from "../auth/AuthProvider";
import { Mark } from "../components/Icons";
import { Callout, ErrorState, Field, Loading } from "../components/ui";

export function SignIn() {
  const auth = useAuth();
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);

  if (auth.status === "loading") return <Loading label="Starting" />;
  if (auth.status === "signedIn") return <Navigate to="/" replace />;
  const oidc = auth.meta?.authentication.oidc;

  return (
    <div className="center-page">
      <main className="card sign-in" aria-labelledby="sign-in-title">
        <div className="card-body stack" style={{ gap: 18, padding: 28 }}>
          <div className="stack" style={{ gap: 14, alignItems: "center", textAlign: "center" }}>
            <Mark size={40} />
            <div>
              <h1 id="sign-in-title">Sign in to Neura</h1>
              <p className="secondary small" style={{ marginTop: 4 }}>
                Deploy and operate robot policies on your own GPU servers.
              </p>
            </div>
          </div>
          {!auth.meta ? (
            <Callout tone="danger" title="The control plane is not reachable">
              Check your network connection and try again.
            </Callout>
          ) : null}
          {oidc ? (
            <button
              type="button"
              className="button primary"
              onClick={() => void auth.signInWithSso().catch((e: unknown) => setError(String(e)))}
            >
              Continue with single sign-on
            </button>
          ) : null}
          <form
            className="stack"
            onSubmit={(event) => {
              event.preventDefault();
              const value = token.trim();
              if (!value.startsWith("ntk_")) {
                setError("API tokens start with ntk_.");
                return;
              }
              auth.signInWithToken(value);
            }}
          >
            <Field
              id="api-token"
              label={oidc ? "Or use an API token" : "API token"}
              hint="Created by an admin under Settings. It is kept only in this browser tab."
              error={error}
            >
              <input
                id="api-token"
                type="password"
                autoComplete="off"
                value={token}
                aria-invalid={error ? true : undefined}
                aria-describedby={`api-token-hint${error ? " api-token-error" : ""}`}
                onChange={(event) => {
                  setToken(event.target.value);
                  setError(null);
                }}
              />
            </Field>
            <button type="submit" className={oidc ? "button" : "button primary"}>
              Sign in with token
            </button>
          </form>
          {auth.meta ? (
            <p className="muted small" style={{ textAlign: "center" }}>
              Control plane {auth.meta.service_version} · API {auth.meta.api_spec_version}
              {auth.meta.environment && auth.meta.environment !== "production"
                ? ` · ${auth.meta.environment}`
                : ""}
            </p>
          ) : null}
        </div>
      </main>
    </div>
  );
}

export function AuthCallback() {
  const auth = useAuth();
  const navigate = useNavigate();
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    if (auth.status === "loading") return;
    auth.completeSso().then(
      () => void navigate("/", { replace: true }),
      (e: unknown) => setError(e),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps -- run once the auth state is known
  }, [auth.status]);
  return (
    <div className="center-page">
      <div className="sign-in">
        {error ? (
          <ErrorState error={error} title="Sign-in failed" />
        ) : (
          <Loading label="Signing in" />
        )}
      </div>
    </div>
  );
}
