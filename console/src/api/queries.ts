// Server state for every screen, cached and refreshed in the background (design §19.6).
// Keys: ["me"]; ["org", orgId, ...] for organization lists; ["p", projectId, ...] for everything
// inside a project (so one change event refreshes the views on screen); ["r", id] for single
// resources outside a known project.
import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryKey,
  type UseQueryOptions,
} from "@tanstack/react-query";

import { api, ifMatch, unwrap, type Schemas } from "./client";

type S = Schemas;
const ALL = 200;

interface Page<T> {
  items: T[];
  page: { next_cursor: string | null };
}

/** Every item of a cursor-paginated list. */
async function all<T>(fetchPage: (cursor: string | undefined) => Promise<Page<T>>): Promise<T[]> {
  const items: T[] = [];
  let cursor: string | undefined;
  for (;;) {
    const page = await fetchPage(cursor);
    items.push(...page.items);
    if (!page.page.next_cursor) return items;
    cursor = page.page.next_cursor;
  }
}

function useApiQuery<T>(
  key: QueryKey,
  fn: () => Promise<T>,
  options: Partial<UseQueryOptions<T>> = {},
) {
  // A query scoped to an organization, project or resource waits for its identifier: never a
  // request with an empty path segment while the identity or the route is still loading.
  const scoped = key.length > 1 && (key[0] === "org" || key[0] === "p");
  const ready = !scoped || (typeof key[1] === "string" && key[1] !== "");
  return useQuery<T>({
    queryKey: key,
    queryFn: fn,
    ...options,
    enabled: ready && (options.enabled ?? true),
  });
}

// --- identity and organizations ------------------------------------------------------------

export const useMe = () => useApiQuery(["me"], async () => unwrap(await api.GET("/v1/me")));

/** The CLI sign-in waiting for a code (design §29.2), looked up before a person approves it. */
export const useLoginCode = (userCode: string | null) =>
  useApiQuery(
    ["login-code", userCode],
    async () =>
      unwrap(
        await api.GET("/v1/login-codes/{user_code}", {
          params: { path: { user_code: userCode ?? "" } },
        }),
      ),
    { enabled: userCode !== null },
  );

export const useOrganization = (orgId: string) =>
  useApiQuery(["org", orgId], async () =>
    unwrap(
      await api.GET("/v1/organizations/{organization_id}", {
        params: { path: { organization_id: orgId } },
      }),
    ),
  );

export const useProjects = (orgId: string) =>
  useApiQuery(["org", orgId, "projects"], () =>
    all(async (cursor) =>
      unwrap(
        await api.GET("/v1/organizations/{organization_id}/projects", {
          params: { path: { organization_id: orgId }, query: { cursor, page_size: ALL } },
        }),
      ),
    ),
  );

export const useMembers = (orgId: string) =>
  useApiQuery(["org", orgId, "members"], () =>
    all(async (cursor) =>
      unwrap(
        await api.GET("/v1/organizations/{organization_id}/members", {
          params: { path: { organization_id: orgId }, query: { cursor, page_size: ALL } },
        }),
      ),
    ),
  );

export const useApiTokens = (orgId: string) =>
  useApiQuery(["org", orgId, "api-tokens"], () =>
    all(async (cursor) =>
      unwrap(
        await api.GET("/v1/organizations/{organization_id}/api-tokens", {
          params: { path: { organization_id: orgId }, query: { cursor, page_size: ALL } },
        }),
      ),
    ),
  );

export const useSupportGrants = (orgId: string) =>
  useApiQuery(["org", orgId, "support-grants"], () =>
    all(async (cursor) =>
      unwrap(
        await api.GET("/v1/organizations/{organization_id}/support-grants", {
          params: { path: { organization_id: orgId }, query: { cursor, page_size: ALL } },
        }),
      ),
    ),
  );

// --- project collections ------------------------------------------------------------------------

type ProjectList =
  | "/v1/projects/{project_id}/sites"
  | "/v1/projects/{project_id}/robots"
  | "/v1/projects/{project_id}/robot-profiles"
  | "/v1/projects/{project_id}/model-artifacts"
  | "/v1/projects/{project_id}/execution-profiles"
  | "/v1/projects/{project_id}/deployments"
  | "/v1/projects/{project_id}/sessions"
  | "/v1/projects/{project_id}/faults"
  | "/v1/projects/{project_id}/audit-records";

function projectList<T>(path: ProjectList, projectId: string): Promise<T[]> {
  return all(async (cursor) => {
    const result = await api.GET(path, {
      params: { path: { project_id: projectId }, query: { cursor, page_size: ALL } },
    });
    return unwrap(result) as Page<T>;
  });
}

const collection = (name: string, path: ProjectList) =>
  function useCollection<T>(projectId: string, options: Partial<UseQueryOptions<T[]>> = {}) {
    return useApiQuery<T[]>(["p", projectId, name], () => projectList<T>(path, projectId), options);
  };

const sites = collection("sites", "/v1/projects/{project_id}/sites");
const robots = collection("robots", "/v1/projects/{project_id}/robots");
const robotProfiles = collection("robot-profiles", "/v1/projects/{project_id}/robot-profiles");
const artifacts = collection("model-artifacts", "/v1/projects/{project_id}/model-artifacts");
const executionProfiles = collection(
  "execution-profiles",
  "/v1/projects/{project_id}/execution-profiles",
);
const deployments = collection("deployments", "/v1/projects/{project_id}/deployments");
const sessions = collection("sessions", "/v1/projects/{project_id}/sessions");
const faults = collection("faults", "/v1/projects/{project_id}/faults");
const audit = collection("audit-records", "/v1/projects/{project_id}/audit-records");

export const useSites = (projectId: string) => sites<S["Site"]>(projectId);
export const useRobots = (projectId: string) => robots<S["Robot"]>(projectId);
export const useRobotProfiles = (projectId: string) => robotProfiles<S["RobotProfile"]>(projectId);
export const useArtifacts = (projectId: string) => artifacts<S["ModelArtifact"]>(projectId);
export const useExecutionProfiles = (projectId: string) =>
  executionProfiles<S["ExecutionProfile"]>(projectId);
export const useDeployments = (projectId: string) => deployments<S["Deployment"]>(projectId);
// Live views also poll, in case the event stream is interrupted (it reconnects by itself).
export const useSessions = (projectId: string) =>
  sessions<S["Session"]>(projectId, { refetchInterval: 5000 });
export const useFaults = (projectId: string) => faults<S["Fault"]>(projectId);
export const useAudit = (projectId: string) => audit<S["AuditRecord"]>(projectId);

export const useProject = (projectId: string) =>
  useApiQuery(["p", projectId, "project"], async () =>
    unwrap(
      await api.GET("/v1/projects/{project_id}", { params: { path: { project_id: projectId } } }),
    ),
  );

// --- resources inside a project -----------------------------------------------------------------

export const useSite = (projectId: string, siteId: string) =>
  useApiQuery(
    ["p", projectId, "site", siteId],
    async () =>
      unwrap(await api.GET("/v1/sites/{site_id}", { params: { path: { site_id: siteId } } })),
    { refetchInterval: 10000 },
  );

export const useServers = (projectId: string, siteId: string, poll = false) =>
  useApiQuery(
    ["p", projectId, "servers", siteId],
    () =>
      all(async (cursor) =>
        unwrap(
          await api.GET("/v1/sites/{site_id}/servers", {
            params: { path: { site_id: siteId }, query: { cursor, page_size: ALL } },
          }),
        ),
      ),
    poll ? { refetchInterval: 2000 } : {},
  );

export const useRobot = (projectId: string, robotId: string) =>
  useApiQuery(["p", projectId, "robot", robotId], async () =>
    unwrap(await api.GET("/v1/robots/{robot_id}", { params: { path: { robot_id: robotId } } })),
  );

export const useDeployment = (projectId: string, deploymentId: string) =>
  useApiQuery(["p", projectId, "deployment", deploymentId], async () =>
    unwrap(
      await api.GET("/v1/deployments/{deployment_id}", {
        params: { path: { deployment_id: deploymentId } },
      }),
    ),
  );

export const useRevisions = (projectId: string, deploymentId: string) =>
  useApiQuery(["p", projectId, "revisions", deploymentId], () =>
    all(async (cursor) =>
      unwrap(
        await api.GET("/v1/deployments/{deployment_id}/revisions", {
          params: { path: { deployment_id: deploymentId }, query: { cursor, page_size: ALL } },
        }),
      ),
    ),
  );

export const useActivationRequests = (projectId: string, deploymentId: string) =>
  useApiQuery(["p", projectId, "activation-requests", deploymentId], () =>
    all(async (cursor) =>
      unwrap(
        await api.GET("/v1/deployments/{deployment_id}/activation-requests", {
          params: { path: { deployment_id: deploymentId }, query: { cursor, page_size: ALL } },
        }),
      ),
    ),
  );

// Evaluations and training jobs run at the site for minutes or hours: poll while any is open.
const openJob = (items: { status: { phase: string } }[] | undefined) =>
  items?.some((x) => x.status.phase === "Pending" || x.status.phase === "Running") ? 3000 : false;

export const useEvaluations = (projectId: string, deploymentId: string) =>
  useApiQuery(
    ["p", projectId, "evaluations", deploymentId],
    () =>
      all(async (cursor) =>
        unwrap(
          await api.GET("/v1/deployments/{deployment_id}/evaluations", {
            params: { path: { deployment_id: deploymentId }, query: { cursor, page_size: ALL } },
          }),
        ),
      ),
    { refetchInterval: (query) => openJob(query.state.data) },
  );

export const useTrainingJobs = (projectId: string, deploymentId: string) =>
  useApiQuery(
    ["p", projectId, "training-jobs", deploymentId],
    () =>
      all(async (cursor) =>
        unwrap(
          await api.GET("/v1/deployments/{deployment_id}/training-jobs", {
            params: { path: { deployment_id: deploymentId }, query: { cursor, page_size: ALL } },
          }),
        ),
      ),
    { refetchInterval: (query) => openJob(query.state.data) },
  );

export const useOperation = (projectId: string, operationId: string | null) =>
  useApiQuery(
    ["p", projectId, "operation", operationId],
    async () =>
      unwrap(
        await api.GET("/v1/operations/{operation_id}", {
          params: { path: { operation_id: operationId ?? "" } },
        }),
      ),
    {
      enabled: operationId !== null,
      refetchInterval: (query) =>
        query.state.data && ["Succeeded", "Failed", "Cancelled"].includes(query.state.data.state)
          ? false
          : 2000,
    },
  );

export const useCompatibility = (
  projectId: string,
  request: S["CompatibilityCheckRequest"] | null,
) =>
  useApiQuery(
    ["p", projectId, "compatibility", request],
    async () =>
      unwrap(
        await api.POST("/v1/projects/{project_id}/compatibility-checks", {
          params: { path: { project_id: projectId } },
          body: request as S["CompatibilityCheckRequest"],
        }),
      ),
    { enabled: request !== null, staleTime: 60_000 },
  );

export type MetricName = S["MetricsQuery"]["metric"];

/** An aggregate metric over the last `windowSeconds`, refreshed every minute (design §18: only
 *  aggregates ever leave a site). Points are [epoch ms, value | null]. */
export const useMetric = (
  projectId: string,
  metric: MetricName,
  windowSeconds = 3600,
  filters: S["MetricsQuery"]["filters"] = {},
) =>
  useApiQuery(
    ["p", projectId, "metric", metric, windowSeconds, filters],
    async () => {
      const end = new Date();
      const start = new Date(end.getTime() - windowSeconds * 1000);
      const step = Math.max(10, Math.round(windowSeconds / 60));
      const result = unwrap(
        await api.POST("/v1/projects/{project_id}/metrics/query", {
          params: { path: { project_id: projectId } },
          body: {
            metric,
            start: start.toISOString().replace(/\.\d+Z$/, "Z"),
            end: end.toISOString().replace(/\.\d+Z$/, "Z"),
            step_seconds: step,
            filters,
          },
        }),
      );
      return (result.series[0]?.points ?? []).map(
        ([t, v]) =>
          [new Date(t as string).getTime(), v as number | null] as [number, number | null],
      );
    },
    { enabled: Boolean(projectId), refetchInterval: 60_000, staleTime: 30_000 },
  );

// --- mutations ------------------------------------------------------------------------------------

/** A mutation that refreshes the given query keys when it settles. */
// --- the fleet (design §29): policies, their versions, rollouts, activity --------------------

export const useWorkspace = (orgId: string) =>
  useApiQuery(["org", orgId, "workspace"], async () =>
    unwrap(await api.GET("/v1/workspace", { params: { query: { organization_id: orgId } } })),
  );

export const usePolicies = (projectId: string) =>
  useApiQuery(["p", projectId, "policies"], () =>
    all(async (cursor) =>
      unwrap(
        await api.GET("/v1/projects/{project_id}/policies", {
          params: { path: { project_id: projectId }, query: { cursor, page_size: ALL } },
        }),
      ),
    ),
  );

export const usePolicy = (projectId: string, policyId: string) =>
  useApiQuery(["p", projectId, "policy", policyId], async () =>
    unwrap(
      await api.GET("/v1/policies/{policy_id}", { params: { path: { policy_id: policyId } } }),
    ),
  );

export const usePolicyVersions = (projectId: string, policyId: string) =>
  useApiQuery(
    ["p", projectId, "policy-versions", policyId],
    () =>
      all(async (cursor) =>
        unwrap(
          await api.GET("/v1/policies/{policy_id}/versions", {
            params: { path: { policy_id: policyId }, query: { cursor, page_size: ALL } },
          }),
        ),
      ),
    { enabled: policyId !== "" },
  );

/** Rollouts, newest first; live ones poll so their steps move on screen. */
export const useRollouts = (projectId: string, query: { robot_id?: string } = {}) =>
  useApiQuery(
    ["p", projectId, "rollouts", query],
    async () =>
      unwrap(
        await api.GET("/v1/projects/{project_id}/rollouts", {
          params: { path: { project_id: projectId }, query: { ...query, page_size: 50 } },
        }),
      ).items,
    { refetchInterval: 3000 },
  );

export const useRollout = (projectId: string, rolloutId: string) =>
  useApiQuery(
    ["p", projectId, "rollout", rolloutId],
    async () =>
      unwrap(
        await api.GET("/v1/rollouts/{rollout_id}", { params: { path: { rollout_id: rolloutId } } }),
      ),
    { refetchInterval: 2000 },
  );

export const useFleetActivity = (
  projectId: string,
  query: { site_id?: string; robot_id?: string; rollout_id?: string } = {},
) =>
  useApiQuery(
    ["p", projectId, "activity", query],
    async () =>
      unwrap(
        await api.GET("/v1/projects/{project_id}/activity", {
          params: { path: { project_id: projectId }, query: { ...query, page_size: 50 } },
        }),
      ).items,
    { refetchInterval: 5000 },
  );

export const usePublisherKeys = (orgId: string) =>
  useApiQuery(["org", orgId, "publisher-keys"], () =>
    all(async (cursor) =>
      unwrap(
        await api.GET("/v1/organizations/{organization_id}/publisher-keys", {
          params: { path: { organization_id: orgId }, query: { cursor, page_size: ALL } },
        }),
      ),
    ),
  );

export function useApiMutation<TInput, TOutput>(
  fn: (input: TInput) => Promise<TOutput>,
  invalidate: QueryKey[] = [],
) {
  const queryClient = useQueryClient();
  return useMutation<TOutput, Error, TInput>({
    mutationFn: fn,
    onSettled: async () => {
      await Promise.all(invalidate.map((queryKey) => queryClient.invalidateQueries({ queryKey })));
    },
  });
}

export { ifMatch };
