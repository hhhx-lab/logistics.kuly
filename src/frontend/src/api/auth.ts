import type { ApiResponses } from "./generated/request-contracts";
import { api } from "./client";

export const authApi = {
  registrationConfig: () => api<ApiResponses["GET /api/v1/auth/registration-config"]["200"]>("/auth/registration-config"),
  sendRegistrationCode: (email: string) => api<ApiResponses["POST /api/v1/auth/register/send-code"]["202"]>("/auth/register/send-code", { method: "POST", body: { email } }),
  register: (account: string, email: string, password: string, verification_code?: string) => api<ApiResponses["POST /api/v1/auth/register"]["201"]>("/auth/register", { method: "POST", body: { account, email, password, ...(verification_code ? { verification_code } : {}) } }),
  login: (account: string, password: string) => api<ApiResponses["POST /api/v1/auth/login"]["200"]>("/auth/login", { method: "POST", body: { account, password } }),
  logout: () => api<void>("/auth/logout", { method: "POST" }),
  me: () => api<ApiResponses["GET /api/v1/me"]["200"]>("/me"),
  disclosure: () => api<ApiResponses["GET /api/v1/data-processing-disclosure"]["200"]>("/data-processing-disclosure"),
};
