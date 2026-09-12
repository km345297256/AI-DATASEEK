import type { FileInfo } from '../api/file';
import type { VisualizationPlugin } from './contract';

/** Public content markers only: never signed URLs, host paths or arbitrary metadata. */
export function filePreviewIdentity(file: FileInfo): string {
  return JSON.stringify([file.file_id, file.filename, file.content_type, file.size, file.upload_date,
    ...['sha256', 'content_sha256', 'dataset_file_version'].map(key => {
      const value = file.metadata?.[key];
      return typeof value === 'string' && value.length <= 256 ? value : null;
    })]);
}

/** Compare the full approved descriptor, including permissions and effective state.
 * Runtime revision alone does not include the user's enabled/disabled preference.
 * Explicit field ordering also makes fresh JSON objects and set ordering harmless.
 */
export function pluginPreviewIdentity(plugin: VisualizationPlugin): string {
  return JSON.stringify([
    plugin.contract_version, plugin.id, plugin.version, plugin.name, plugin.description,
    [...(plugin.extensions ?? [])].sort(), [...(plugin.filenames ?? [])].sort(),
    plugin.view_kind, plugin.adapter, plugin.reader,
    [...(plugin.capabilities?.operations ?? [])].sort(), plugin.capabilities?.input_mode, plugin.capabilities?.shared,
    plugin.default_enabled, plugin.enabled, plugin.priority, [...(plugin.permissions ?? [])].sort(),
    plugin.limits?.max_input_bytes, plugin.limits?.max_output_bytes,
  ]);
}
