import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Suspense, lazy, useEffect, type ComponentType, type ReactNode } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation, useParams } from "react-router";

import { ApiError } from "./api/client";
import { AuthProvider, useAuth } from "./auth/AuthProvider";
import { Layout, OrgLayout } from "./components/Layout";
import { ToastProvider } from "./components/Toasts";
import { Loading } from "./components/ui";
import { AuthCallback, SignIn } from "./pages/SignIn";
import { Projects, Start, localStorageSet } from "./pages/Start";

// Screens load on first visit, so the first paint (sign-in, home) stays small.
const page = <K extends string>(load: () => Promise<Record<K, ComponentType>>, name: K) =>
  lazy(async () => ({ default: (await load())[name] }));
const Overview = page(() => import("./pages/Home"), "Home");
const Sites = page(() => import("./pages/Sites"), "Sites");
const SitePage = page(() => import("./pages/Site"), "SitePage");
const Robots = page(() => import("./pages/Robots"), "Robots");
const RobotPage = page(() => import("./pages/Robots"), "RobotPage");
const Models = page(() => import("./pages/Models"), "Models");
const ContractPage = page(() => import("./pages/Models"), "ContractPage");
const Capacity = page(() => import("./pages/Observe"), "Capacity");
const Sessions = page(() => import("./pages/Observe"), "Sessions");
const Faults = page(() => import("./pages/Observe"), "Faults");
const Activity = page(() => import("./pages/Observe"), "Activity");
const Deployments = page(() => import("./pages/Releases"), "Releases");
const DeploymentPage = page(() => import("./pages/Release"), "ReleasePage");
const Settings = page(() => import("./pages/Settings"), "Settings");
const Fleet = page(() => import("./pages/Fleet"), "Fleet");
const Policies = page(() => import("./pages/Policies"), "Policies");
const PolicyPage = page(() => import("./pages/Policies"), "PolicyPage");
const Rollouts = page(() => import("./pages/Rollouts"), "Rollouts");
const RolloutPage = page(() => import("./pages/Rollouts"), "RolloutPage");
const Activate = page(() => import("./pages/Activate"), "Activate");

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 5_000,
        // Retry what can succeed later; a 4xx will not.
        retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 3,
        refetchOnWindowFocus: true,
      },
    },
  });
}

function RequireAuth({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const location = useLocation();
  if (auth.status === "loading") return <Loading label="Starting" />;
  if (auth.status === "signedOut")
    return <Navigate to="/sign-in" replace state={{ from: location }} />;
  return children;
}

/** Remember the last project per organization, so Neura reopens where you were. */
function RememberProject() {
  const { orgId = "", projectId = "" } = useParams();
  useEffect(() => {
    if (orgId && projectId) localStorageSet(`neura.project.${orgId}`, projectId);
  }, [orgId, projectId]);
  return null;
}

function RedirectRelease() {
  const { orgId = "", projectId = "", deploymentId = "" } = useParams();
  return <Navigate to={`/o/${orgId}/p/${projectId}/deployments/${deploymentId}`} replace />;
}

/** Each screen sets the document title, so tabs and assistive tech say where you are. */
function Title({ text, children }: { text: string; children: ReactNode }) {
  useEffect(() => {
    document.title = `${text} · Neura`;
  }, [text]);
  return <Suspense fallback={<Loading />}>{children}</Suspense>;
}

export function AppRoutes() {
  const screen = (text: string, element: ReactNode) => <Title text={text}>{element}</Title>;
  return (
    <Routes>
      <Route path="/sign-in" element={screen("Sign in", <SignIn />)} />
      <Route path="/auth/callback" element={<AuthCallback />} />
      <Route
        path="/activate"
        element={<RequireAuth>{screen("Sign in the command line", <Activate />)}</RequireAuth>}
      />
      <Route
        path="/"
        element={
          <RequireAuth>
            <Start />
          </RequireAuth>
        }
      />
      <Route
        path="/o/:orgId"
        element={
          <RequireAuth>
            <OrgLayout />
          </RequireAuth>
        }
      >
        <Route path="projects" element={screen("Projects", <Projects />)} />
        <Route path="settings" element={screen("Settings", <Settings />)} />
      </Route>
      <Route
        path="/o/:orgId/p/:projectId"
        element={
          <RequireAuth>
            <RememberProject />
            <Layout />
          </RequireAuth>
        }
      >
        <Route index element={screen("Fleet", <Fleet />)} />
        <Route path="policies" element={screen("Policies", <Policies />)} />
        <Route path="policies/:policyId" element={screen("Policy", <PolicyPage />)} />
        <Route path="rollouts" element={screen("Rollouts", <Rollouts />)} />
        <Route path="rollouts/:rolloutId" element={screen("Rollout", <RolloutPage />)} />
        <Route path="overview" element={screen("Overview", <Overview />)} />
        <Route path="deployments" element={screen("Deployments", <Deployments />)} />
        <Route
          path="deployments/:deploymentId"
          element={screen("Deployment", <DeploymentPage />)}
        />
        <Route path="robots" element={screen("Robots", <Robots />)} />
        <Route path="robots/:robotId" element={screen("Robot", <RobotPage />)} />
        <Route path="sites" element={screen("Sites & servers", <Sites />)} />
        <Route path="sites/:siteId" element={screen("Site", <SitePage />)} />
        <Route path="sessions" element={screen("Live sessions", <Sessions />)} />
        <Route path="faults" element={screen("Faults", <Faults />)} />
        <Route path="models" element={screen("Models & contracts", <Models />)} />
        <Route path="models/:kind/:contractId" element={screen("Contract", <ContractPage />)} />
        <Route path="capacity" element={screen("Capacity", <Capacity />)} />
        <Route path="activity" element={screen("Activity", <Activity />)} />
        {/* Earlier addresses keep working. */}
        <Route path="releases" element={<Navigate to="../deployments" replace />} />
        <Route path="releases/:deploymentId" element={<RedirectRelease />} />
        <Route
          path="execution-profiles"
          element={<Navigate to="../models?tab=execution-profiles" replace />}
        />
        <Route path="audit" element={<Navigate to="../activity?tab=audit" replace />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

export function App({ queryClient = makeQueryClient() }: { queryClient?: QueryClient }) {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <ToastProvider>
          <BrowserRouter>
            <AppRoutes />
          </BrowserRouter>
        </ToastProvider>
      </AuthProvider>
    </QueryClientProvider>
  );
}
