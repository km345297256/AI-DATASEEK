/** Server-owned projection of a confirmed first-party execution receipt.
 * Identity is opaque: never reconstruct it from paths, labels or tool output.
 */
export interface ProgramAttemptView {
  version: 1;
  identity: string;
  state: 'succeeded' | 'failed';
  returncode: number;
}
