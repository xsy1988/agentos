/** 技能沉淀 API */
import { api } from "./client";
import type { ProposalOut } from "./types";

export const skillsApi = {
  proposals: (status?: string) =>
    api.get<ProposalOut[]>("/skills/proposals", { status }),
  proposal: (id: string) => api.get<ProposalOut>(`/skills/proposals/${id}`),
  approve: (id: string, reviewNote?: string) =>
    api.post(`/skills/proposals/${id}/approve`, { review_note: reviewNote }),
  reject: (id: string, reviewNote?: string) =>
    api.post(`/skills/proposals/${id}/reject`, { review_note: reviewNote }),
  review: (runId: string, trigger = "success") =>
    api.post(`/skills/review/${runId}`, { trigger }),
};
