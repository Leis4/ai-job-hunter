export type ScoreInfo = {
  skill_match?: number; career_fit?: number; utility?: number; confidence?: number; match_score?: number;
  invite_realism?: number; schedule_fit?: number; growth_opportunity?: number; experience_gap_years?: number | null;
};

export type Job = {
  id: string; title: string; company: string; source: string; url: string;
  location: string; work_mode: string; salary_min?: number | null; salary_max?: number | null;
  currency?: string; description: string; published_at: string; fetched_at: string;
  score: number; score_low: number; score_high: number; eligible: boolean;
  needs_review: boolean; favorite: boolean; status: string; reason: string;
  letter: string; letter_source: string;
  eligibility: "eligible" | "review" | "rejected" | "weak";
  category?: "strong" | "opportunity" | "rejected" | "";
  category_reasons?: string[];
  failed_constraints: string[]; seniority: string; scores: ScoreInfo;
  realism: { value: number; reasons: string[]; positive?: string[]; confidence?: number; note?: string };
  commercial_experience: { required: boolean; signals: string[]; candidate_months: number };
  schedule?: { status: string; signals: string[] };
  location_mismatch?: boolean;
  enrichment?: {
    source_url?: string; post_url?: string; kind?: string; fetched_at?: string;
    missing?: string[];
    fields?: Record<string, { value: any; source: string }>;
    links?: { url: string; text: string; host: string; score: number; kind: string }[];
  };
  apply_url?: string; source_post_url?: string; enriched_at?: string;
  personal_probability?: {
    available: boolean; personal_probability?: number | null; point_estimate_for_display: number;
    interval_90?: number[] | null; confidence: number; confidence_label: string;
    n: number; successes: number; failures: number; reason: string; method: string;
  };
  publication_count: number;
  skills: { matched: string[]; missing: { name: string; weight?: number; requirement?: string }[]; required_missing: string[] };
  experience: { required_years?: number | null; candidate_years?: number; hard_fail?: boolean; seniority_fail?: boolean };
  education: { required?: boolean; strict?: boolean; confirmed?: boolean };
  license: { required?: boolean; confirmed?: boolean };
  factors: Record<string, { value: number; weight: number; sigma?: number; low?: number; high?: number }>;
  interval: { low?: number; high?: number };
  penalty?: number | null; hard_fail?: boolean; threshold?: number;
  red_flags: string[]; explanation: { factor: string; score: number; weight?: number; contribution: number; gap?: number }[];
};

export type Dashboard = {
  summary: any; funnel: { key: string; label: string; count: number; share: number }[];
  series: { date: string; jobs: number; letters: number; sent: number; replies: number }[];
  sources: any[]; score_bands: any[]; skills: any[]; posterior: any; calibration: any;
  events: { id: number; type: string; created_at: string; job_title?: string; payload?: string }[];
  skip_reasons: { reason: string; count: number }[];
  top_jobs: Job[]; prospects_count?: number; runner: { running: boolean; elapsed: number; report?: any; error?: string; stage?: string; progress?: any };
  last_update?: string; llm: any; profile: { verified_skills: number; has_resume: boolean; draft_status?: string };
  queue: any; threshold?: number;
  automation?: any;
  source_health?: Record<string, any>;
  metrics?: { source_types: number; telegram_active: number; incomplete: number; awaiting: number; submitted: number };
  startups?: { stats: StartupStats; top: StartupLead[] };
  actions?: { ready_to_submit: number; ready_to_review: number; needs_action: number;
    submitted: number; unverified: number; replies?: number;
    applications: Record<string, number>; leads: Record<string, number> };
};

export type StartupLead = {
  id: string; kind: string; kind_label: string; status: string; state_label: string;
  company: string; product: string; website: string; description: string; stack: string[];
  contact: string; contact_kind: string; source: string; source_url: string;
  evidence_url: string; evidence_text: string; hiring_status: string; pay_status: string;
  pay_label: string; work_mode: string; location: string; role: string;
  fit_score: number; hiring_evidence: number; compensation_confidence: number;
  rationale: { facts?: string[]; matches?: string[]; gaps?: string[]; assumptions?: string[]; notes?: string[] };
  outreach: string; outreach_subject: string; outreach_updated_at: string;
  readiness?: string; readiness_label?: string; readiness_reason?: string;
  published_at: string; last_checked_at: string; other_sources: { source: string; url: string }[];
  founders: string[]; badges: string[]; is_paid: boolean;
};

export type StartupStats = {
  total: number; by_kind: Record<string, number>; by_state: Record<string, number>;
  paid: number; unpaid: number; hiring: number; drafts: number; awaiting: number;
  contacted: number; sources: Record<string, number>; last_checked: string;
};

export type ApplicationsData = {
  items: any[]; stats: any;
  readiness?: { applications: Record<string, number>; leads: Record<string, number>;
    ready_to_submit: number; ready_to_review: number; needs_action: number;
    submitted: number; unverified: number };
  followups?: any[];
};
export type OnboardingData = {
  draft: any; files: string[]; runner: { running: boolean; kind: string; elapsed: number; error: string };
  qwen: { available: boolean; reason: string; model: string }; aitunnel: { available: boolean };
};
export type ResumeData = {
  resume: any; profile: any; variants: Record<string, { label: string; skills: number; projects: number }>;
  versions: { name: string; created: string; size: number }[]; completeness: any; facts: any;
};
export type SettingsData = {
  settings: any; profile: any; resume_stats: any; llm: any; qwen: any; submission: any;
  candidate_memory: any; memory: any; calibration: any;
  dashboard: Dashboard; math_operations: Record<string, any>;
  source_status: Record<string, {
    label: string; description: string; configured: boolean; available: boolean;
    reason: string; last_status: string; last_error: string; last_run_at: string; items: number;
  }>;
};
