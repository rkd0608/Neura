// Who is signed in, and how (design §20.7). SSO uses OIDC authorization code + PKCE with the
// issuer and client published at /v1/meta; API tokens are for development and automation.
// Credentials live in sessionStorage: closing the tab signs out.
import { UserManager, WebStorageStateStore } from "oidc-client-ts";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { configureAuth } from "../api/client";
import type { Schemas } from "../api/client";

type Meta = Schemas["ServiceMeta"];
type OidcSettings = NonNullable<NonNullable<Meta["authentication"]>["oidc"]>;

const TOKEN_KEY = "neura.apiToken";

export interface AuthState {
  status: "loading" | "signedOut" | "signedIn";
  method: "token" | "oidc" | null;
  meta: Meta | null;
  signInWithToken: (token: string) => void;
  signInWithSso: () => Promise<void>;
  completeSso: () => Promise<void>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

function readToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

function writeToken(token: string | null): void {
  try {
    if (token === null) sessionStorage.removeItem(TOKEN_KEY);
    else sessionStorage.setItem(TOKEN_KEY, token);
  } catch {
    // Storage unavailable (private mode): the credential lives only in memory for this page.
  }
}

function userManager(oidc: OidcSettings): UserManager {
  return new UserManager({
    authority: oidc.issuer,
    client_id: oidc.client_id,
    redirect_uri: `${window.location.origin}/auth/callback`,
    post_logout_redirect_uri: `${window.location.origin}/sign-in`,
    response_type: "code",
    scope: oidc.scopes.join(" "),
    ...(oidc.audience ? { extraQueryParams: { audience: oidc.audience } } : {}),
    userStore: new WebStorageStateStore({ store: window.sessionStorage }),
    automaticSilentRenew: true,
  });
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [token, setToken] = useState<string | null>(readToken);
  const [oidcToken, setOidcToken] = useState<string | null>(null);
  const [manager, setManager] = useState<UserManager | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Read through a function, so the checks are not narrowed away: the cleanup flips it while
    // the request is in flight.
    const run = { cancelled: false };
    const cancelled = () => run.cancelled;
    void (async () => {
      try {
        const response = await fetch("/v1/meta");
        const body = (await response.json()) as Meta;
        if (cancelled()) return;
        setMeta(body);
        const oidc = body.authentication.oidc;
        if (oidc) {
          const um = userManager(oidc);
          setManager(um);
          const user = await um.getUser();
          if (!cancelled() && user && !user.expired) setOidcToken(user.access_token);
          um.events.addUserLoaded((u) => setOidcToken(u.access_token));
          um.events.addUserUnloaded(() => setOidcToken(null));
          um.events.addAccessTokenExpired(() => setOidcToken(null));
        }
      } catch {
        // The API is unreachable; the sign-in page says so.
      } finally {
        if (!cancelled()) setLoading(false);
      }
    })();
    return () => {
      run.cancelled = true;
    };
  }, []);

  const credential = token ?? oidcToken;

  const signOut = useCallback(async () => {
    writeToken(null);
    setToken(null);
    if (manager && oidcToken) {
      setOidcToken(null);
      await manager.removeUser();
    }
  }, [manager, oidcToken]);

  useEffect(() => {
    configureAuth(
      () => credential,
      () => void signOut(),
    );
  }, [credential, signOut]);

  const value = useMemo<AuthState>(
    () => ({
      status: loading ? "loading" : credential ? "signedIn" : "signedOut",
      method: token ? "token" : oidcToken ? "oidc" : null,
      meta,
      signInWithToken: (value: string) => {
        writeToken(value);
        setToken(value);
      },
      signInWithSso: async () => {
        if (!manager) throw new Error("SSO is not configured for this control plane");
        await manager.signinRedirect();
      },
      completeSso: async () => {
        if (!manager) throw new Error("SSO is not configured for this control plane");
        const user = await manager.signinRedirectCallback();
        setOidcToken(user.access_token);
      },
      signOut,
    }),
    [loading, credential, token, oidcToken, meta, manager, signOut],
  );

  return <AuthContext value={value}>{children}</AuthContext>;
}

export function useAuth(): AuthState {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
