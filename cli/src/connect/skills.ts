/**
 * Sutr Skills live in their own repository and are installed by the user, not
 * by this CLI. Sutr works without them; they teach an agent the conventions the
 * tool schemas cannot express (approval URLs, `additional_info`, when to poll).
 *
 * The CLI only ever *shows* this command. It does not run it, and does not
 * check whether the repository exists — an unreachable skills repo must never
 * be able to fail a connection that already succeeded.
 */
export const SKILLS_REPOSITORY = "https://github.com/sutr-dev/sutr-skills";
export const SKILLS_INSTALL_COMMAND = "npx skills add sutr-dev/sutr-skills";
