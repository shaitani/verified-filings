// Short names for the Web Server's contract. Every type here comes from the generated
// openapi.d.ts (`npm run api:types`); none is written by hand, so none can drift.
import type { components, paths } from './openapi';

type Schemas = components['schemas'];

// Up, and able to reach the database: { status: "ok" } -- an unnamed shape, so read off its route.
export type Health = paths['/api/health']['get']['responses'][200]['content']['application/json'];

// A reply and its parts (DESIGN §1).
export type Reply = Schemas['Reply'];
export type Part = Schemas['Part'];
export type Refusal = Schemas['Refusal'];
export type Ask = Schemas['Ask'];
export type Option = Schemas['Option'];

// The figures (DESIGN §5, §6).
export type AnswerView = Schemas['AnswerView'];
export type AnswerRow = Schemas['AnswerRow'];
export type CitationView = Schemas['CitationView'];
export type Note = Schemas['Note'];
export type StatView = Schemas['StatView'];
export type LineView = Schemas['LineView'];
export type BarView = Schemas['BarView'];
export type View = StatView | LineView | BarView;

// Conversations and jobs (DESIGN §4).
export type ConversationSummary = Schemas['ConversationSummary'];
export type ConversationView = Schemas['ConversationView'];
export type RoundView = Schemas['RoundView'];
export type GivenAnswer = Schemas['GivenAnswer'];
export type JobCreated = Schemas['JobCreated'];
export type JobView = Schemas['JobView'];
export type JobEvent = Schemas['JobEvent'];
export type StageEvent = Schemas['StageEvent'];
export type JobStage = StageEvent['stage']; // queued, parsing, mapping, fetching, presenting
export type JobStatus = RoundView['status']; // the stages, then done or failed

// What the browser sends.
export type NewConversationIn = Schemas['NewConversationIn'];
export type AnswersIn = Schemas['AnswersIn'];
export type AnswerIn = Schemas['OptionAnswerIn']; // a union once free text joins (§3)
export type FeedbackIn = Schemas['FeedbackIn'];
export type UserCreate = Schemas['UserCreate'];
export type GitHubAuthorize = Schemas['OAuth2AuthorizeResponse'];
export type GitHubInviteIn = Schemas['GitHubInviteIn'];

// Who is signed in.
export type UserRead = Schemas['UserRead'];

// Administration (DESIGN §13): administrators only.
export type AdminUser = Schemas['AdminUser'];
export type AdminInvite = Schemas['AdminInvite'];
export type InviteStatus = AdminInvite['status'];
export type InviteCreated = Schemas['InviteCreated'];
export type NewInviteIn = Schemas['NewInviteIn'];
export type PasswordReset = Schemas['PasswordReset'];
