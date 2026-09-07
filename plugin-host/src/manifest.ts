import { lstat, readFile, readdir, realpath, stat } from 'node:fs/promises'
import { join, relative, resolve, sep } from 'node:path'
import { TextDecoder } from 'node:util'

import { Ajv } from 'ajv'

import { CatalogValidationError } from './errors.js'
import { cloneJson, sha256, stableStringify } from './json.js'
import {
  TOOL_CONTRACT_VERSION,
  TOOL_EFFECTS,
  type ToolDescriptor,
  type ToolEffect,
  type ToolExecutionDescriptor,
  type ToolPresentationDescriptor,
  type ToolPresentationKind,
  type ValidatedPlugin,
} from './types.js'

const PLUGIN_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]*$/
const PLUGIN_VERSION = /^[0-9A-Za-z][0-9A-Za-z.+_-]*$/
const TOOL_NAME = /^[A-Za-z_][A-Za-z0-9_-]{0,63}$/
const MAX_TOOL_TIMEOUT_SECONDS = 120
const JSON_SCHEMA_DIALECT = 'http://json-schema.org/draft-07/schema#'
const TOOL_CONCURRENCY = new Set(['exclusive', 'parallel'])
const TOOL_PRESENTATION_KINDS = new Set([
  'auto',
  'generic',
  'table',
  'chart',
  'map',
  'image',
  'artifact',
  'log',
])
const TOOL_EFFECT_SET = new Set<string>(TOOL_EFFECTS)
const EXECUTION_FIELDS = new Set([
  'timeout_seconds',
  'cancellable',
  'concurrency',
  'effects',
  'permissions',
  'credentials',
])
const PRESENTATION_FIELDS = new Set(['kind', 'title', 'description'])
const TOOL_FIELDS = new Set([
  'contract_version',
  'name',
  'description',
  'parameters',
  'output_schema',
  'execution',
  'presentation',
  'scopes',
  'timeout_seconds',
])
// These Agent capabilities are not plugin extension points. The intentionally
// migrated scientific_* names are absent because ShellToolkit hides those
// legacy definitions when the Cordis toolkit is active.
const RESERVED_AGENT_TOOL_NAMES = new Set([
  'browser_click',
  'browser_console_exec',
  'browser_console_view',
  'browser_input',
  'browser_move_mouse',
  'browser_navigate',
  'browser_press_key',
  'browser_restart',
  'browser_scroll_down',
  'browser_scroll_up',
  'browser_select_option',
  'browser_view',
  'code_mode_run',
  'dataset_quicklook',
  'dataset_unpack',
  'file_find_by_name',
  'file_find_in_content',
  'file_read',
  'file_str_replace',
  'file_write',
  'info_search_web',
  'inspect_dataset_catalog',
  'list_dataset_files',
  'message_ask_user',
  'message_notify_user',
  'mcp_list_tools',
  'resolve_dataset_file',
  'shell_exec',
  'shell_kill_process',
  'shell_read',
  'shell_run',
  'shell_view',
  'shell_wait',
  'shell_write',
  'shell_write_to_process',
  'write_stdin',
  'skill_create_from_session',
  'skill_list',
  'skill_list_resources',
  'skill_read',
  'skill_read_reference',
  'skill_read_script',
  'tool_catalog_search',
  'tool_catalog_load',
])

const PORTABLE_REGEX_ESCAPES = new Set('\\.^$*+?()[]{}|/'.split(''))
const PORTABLE_REGEX_QUANTIFIER = /^\{([0-9]{1,6})(?:,([0-9]{0,6}))?\}/
const MAX_PORTABLE_REGEX_LENGTH = 2048

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function fail(label: string, message: string): never {
  throw new CatalogValidationError(`${label}: ${message}`)
}

function validatePortablePattern(pattern: string, label: string, field: string): void {
  if ([...pattern].length > MAX_PORTABLE_REGEX_LENGTH) {
    fail(label, `${field} exceeds the portable regular-expression length limit`)
  }

  let index = 0
  let inCharacterClass = false
  const groupStack: Array<{ quantified: boolean, alternation: boolean }> = []
  let lastClosedGroup: { quantified: boolean, alternation: boolean } | undefined
  while (index < pattern.length) {
    const character = pattern[index]!
    const codeUnit = pattern.charCodeAt(index)
    if (
      (codeUnit >= 0xd800 && codeUnit <= 0xdbff
        && !(index + 1 < pattern.length
          && pattern.charCodeAt(index + 1) >= 0xdc00
          && pattern.charCodeAt(index + 1) <= 0xdfff))
      || (codeUnit >= 0xdc00 && codeUnit <= 0xdfff
        && !(index > 0
          && pattern.charCodeAt(index - 1) >= 0xd800
          && pattern.charCodeAt(index - 1) <= 0xdbff))
    ) {
      fail(label, `${field} contains an invalid regular-expression surrogate`)
    }

    if (character === '\\') {
      if (index + 1 >= pattern.length) {
        fail(label, `${field} ends with a regular-expression escape`)
      }
      if (!PORTABLE_REGEX_ESCAPES.has(pattern[index + 1]!)) {
        fail(label, `${field} uses a non-portable regular-expression escape`)
      }
      lastClosedGroup = undefined
      index += 2
      continue
    }

    if (inCharacterClass) {
      if (character === '[') {
        fail(label, `${field} uses a nested regular-expression character class`)
      }
      if (character === ']') {
        inCharacterClass = false
        lastClosedGroup = undefined
      }
      index += 1
      continue
    }

    if (character === '[') {
      inCharacterClass = true
      let first = index + 1
      if (first < pattern.length && pattern[first] === '^') first += 1
      if (first >= pattern.length || pattern[first] === ']') {
        fail(label, `${field} uses an empty regular-expression character class`)
      }
      index += 1
      continue
    }
    if (character === ']') {
      fail(label, `${field} has an unmatched regular-expression character class`)
    }
    if (character === '(') {
      if (pattern.slice(index, index + 3) === '(?:') {
        groupStack.push({ quantified: false, alternation: false })
        lastClosedGroup = undefined
        index += 3
        continue
      }
      if (pattern[index + 1] === '?') {
        fail(label, `${field} uses a non-portable regular-expression group`)
      }
      groupStack.push({ quantified: false, alternation: false })
      lastClosedGroup = undefined
      index += 1
      continue
    }
    if (character === ')') {
      lastClosedGroup = groupStack.pop()
      const parent = groupStack.at(-1)
      if (lastClosedGroup && parent) {
        parent.quantified ||= lastClosedGroup.quantified
        parent.alternation ||= lastClosedGroup.alternation
      }
      index += 1
      continue
    }
    if (character === '|') {
      const group = groupStack.at(-1)
      if (group) group.alternation = true
      lastClosedGroup = undefined
      index += 1
      continue
    }
    if (character === '{') {
      const quantifier = pattern.slice(index).match(PORTABLE_REGEX_QUANTIFIER)
      if (!quantifier) {
        fail(label, `${field} uses a non-portable regular-expression brace`)
      }
      const minimum = Number(quantifier[1])
      const maximum = quantifier[2]
      if (maximum && minimum > Number(maximum)) {
        fail(label, `${field} has a reversed regular-expression quantifier`)
      }
      const repeats = maximum === undefined
        ? minimum > 1
        : maximum === '' || Number(maximum) > 1
      if (repeats && lastClosedGroup
        && (lastClosedGroup.quantified || lastClosedGroup.alternation)) {
        fail(label, `${field} uses an unsafe nested regular-expression quantifier`)
      }
      const group = groupStack.at(-1)
      if (group) group.quantified = true
      lastClosedGroup = undefined
      index += quantifier[0].length
      if (pattern[index] === '+') {
        fail(label, `${field} uses a possessive regular-expression quantifier`)
      }
      continue
    }
    if (character === '}') {
      fail(label, `${field} uses a non-portable regular-expression brace`)
    }
    if ('*+?'.includes(character) && pattern[index + 1] === '+') {
      fail(label, `${field} uses a possessive regular-expression quantifier`)
    }
    if ('*+?'.includes(character)) {
      if ('*+'.includes(character) && lastClosedGroup
        && (lastClosedGroup.quantified || lastClosedGroup.alternation)) {
        fail(label, `${field} uses an unsafe nested regular-expression quantifier`)
      }
      const group = groupStack.at(-1)
      if (group) group.quantified = true
      lastClosedGroup = undefined
      index += 1
      continue
    }
    lastClosedGroup = undefined
    index += 1
  }
  try {
    new RegExp(pattern, 'u')
  } catch {
    fail(label, `${field} is not a compilable regular expression`)
  }
  if (hasUnseparatedUnboundedRepetitions(pattern)) {
    fail(label, `${field} uses ambiguous adjacent regular-expression repetitions`)
  }
}

function hasUnseparatedUnboundedRepetitions(pattern: string): boolean {
  let index = 0
  let unboundedSinceSeparator = false
  while (index < pattern.length) {
    const character = pattern[index]!
    if ('^$()'.includes(character)) {
      index += 1
      continue
    }
    if (character === '|') {
      unboundedSinceSeparator = false
      index += 1
      continue
    }

    let atomEnd: number
    if (character === '\\') {
      atomEnd = index + 2
    } else if (character === '[') {
      atomEnd = index + 1
      while (atomEnd < pattern.length) {
        if (pattern[atomEnd] === '\\') {
          atomEnd += 2
          continue
        }
        atomEnd += 1
        if (pattern[atomEnd - 1] === ']') break
      }
    } else if ('*+?{}'.includes(character)) {
      index += 1
      continue
    } else {
      atomEnd = index + 1
    }

    let quantifierEnd = atomEnd
    let minimum = 1
    let maximum: number | undefined = 1
    const quantifier = pattern[quantifierEnd]
    if (quantifier === '*') {
      minimum = 0
      maximum = undefined
      quantifierEnd += 1
    } else if (quantifier === '+') {
      minimum = 1
      maximum = undefined
      quantifierEnd += 1
    } else if (quantifier === '?') {
      minimum = 0
      maximum = 1
      quantifierEnd += 1
    } else if (quantifier === '{') {
      const match = pattern.slice(quantifierEnd).match(PORTABLE_REGEX_QUANTIFIER)
      if (match) {
        minimum = Number(match[1])
        maximum = match[2] === undefined
          ? minimum
          : match[2] === ''
            ? undefined
            : Number(match[2])
        quantifierEnd += match[0].length
      }
    }
    if (pattern[quantifierEnd] === '?') quantifierEnd += 1

    if (maximum === undefined) {
      if (unboundedSinceSeparator) return true
      unboundedSinceSeparator = true
    } else if (minimum >= 1) {
      unboundedSinceSeparator = false
    }
    index = quantifierEnd
  }
  return false
}

function resolveLocalJsonPointer(
  root: Record<string, unknown>,
  reference: string,
  label: string,
  field: string,
): void {
  if (reference === '#') return
  if (!reference.startsWith('#/') || reference.includes('%')) {
    fail(label, `${field} references must be local JSON pointers`)
  }
  let current: unknown = root
  for (const rawToken of reference.slice(2).split('/')) {
    if (/~(?![01])/.test(rawToken)) {
      fail(label, `${field} contains an invalid JSON pointer escape`)
    }
    const token = rawToken.replaceAll('~1', '/').replaceAll('~0', '~')
    if (isRecord(current) && Object.prototype.hasOwnProperty.call(current, token)) {
      current = current[token]
      continue
    }
    if (Array.isArray(current) && /^(?:0|[1-9][0-9]*)$/.test(token)) {
      const index = Number(token)
      if (index < current.length) {
        current = current[index]
        continue
      }
    }
    fail(label, `${field} contains an unresolved JSON pointer`)
  }
}

function validatePortableSchema(
  root: Record<string, unknown>,
  label: string,
  field: string,
): void {
  const pending: unknown[] = [root]
  while (pending.length) {
    const value = pending.pop()
    if (Array.isArray(value)) {
      pending.push(...value)
      continue
    }
    if (!isRecord(value)) continue

    if (value.$ref !== undefined) {
      if (typeof value.$ref !== 'string') fail(label, `${field} $ref must be a string`)
      resolveLocalJsonPointer(root, value.$ref, label, field)
    }
    if (value.pattern !== undefined) {
      if (typeof value.pattern !== 'string') fail(label, `${field} pattern must be a string`)
      validatePortablePattern(value.pattern, label, field)
    }
    if (isRecord(value.patternProperties)) {
      for (const pattern of Object.keys(value.patternProperties)) {
        validatePortablePattern(pattern, label, field)
      }
    }
    pending.push(...Object.values(value))
  }
}

function requireNonEmptyString(
  value: unknown,
  label: string,
  field: string,
): string {
  if (typeof value !== 'string' || !value.trim()) {
    fail(label, `${field} must be a non-empty string`)
  }
  return value
}

function validateParameters(
  value: unknown,
  label: string,
  ajv: Ajv,
): Record<string, unknown> {
  if (!isRecord(value) || value.type !== 'object') {
    fail(label, 'parameters must be a JSON Schema object with type "object"')
  }
  validateSchemaDialect(value, label, 'parameters')
  try {
    if (!ajv.validateSchema(value)) {
      fail(label, 'parameters is not a valid JSON Schema')
    }
    validatePortableSchema(value, label, 'parameters')
    // Compilation also catches unresolved references and invalid combinations
    // that meta-schema validation alone does not reject.
    ajv.compile(value)
  } catch (error) {
    if (error instanceof CatalogValidationError) throw error
    fail(label, 'parameters is not a compilable JSON Schema')
  }
  return cloneJson(value)
}

function validateOutputSchema(
  value: unknown,
  label: string,
  ajv: Ajv,
): Record<string, unknown> | null {
  // Legacy handlers currently write heterogeneous process output. `null`
  // preserves that truth instead of claiming an unrestricted schema was
  // validated. Interceptors may validate only non-null schemas.
  if (value === undefined || value === null) return null
  if (!isRecord(value)) {
    fail(label, 'output_schema must be a JSON Schema object or null')
  }
  validateSchemaDialect(value, label, 'output_schema')
  try {
    if (!ajv.validateSchema(value)) {
      fail(label, 'output_schema is not a valid JSON Schema')
    }
    validatePortableSchema(value, label, 'output_schema')
    ajv.compile(value)
  } catch (error) {
    if (error instanceof CatalogValidationError) throw error
    fail(label, 'output_schema is not a compilable JSON Schema')
  }
  return cloneJson(value)
}

function validateSchemaDialect(
  value: Record<string, unknown>,
  label: string,
  field: string,
): void {
  if (value.$schema !== undefined && value.$schema !== JSON_SCHEMA_DIALECT) {
    fail(label, `${field} must use JSON Schema draft-07`)
  }
}

function validateScopes(value: unknown, label: string): string[] {
  if (value === undefined) return []
  if (!Array.isArray(value)) fail(label, 'scopes must be an array of strings')
  const scopes: string[] = []
  for (const scope of value) {
    if (typeof scope !== 'string' || !scope.trim()) {
      fail(label, 'scopes must contain only non-empty strings')
    }
    if (scopes.includes(scope)) fail(label, `duplicate scope: ${scope}`)
    scopes.push(scope)
  }
  return scopes
}

function validateTimeout(value: unknown, label: string): number {
  if (value === undefined) return 90
  if (
    !Number.isSafeInteger(value)
    || (value as number) < 1
    || (value as number) > MAX_TOOL_TIMEOUT_SECONDS
  ) {
    fail(label, `timeout_seconds must be an integer from 1 to ${MAX_TOOL_TIMEOUT_SECONDS}`)
  }
  return value as number
}

function validateStringList(
  value: unknown,
  label: string,
  field: string,
  fallback: readonly string[],
): string[] {
  if (value === undefined) return [...fallback]
  if (!Array.isArray(value)) fail(label, `${field} must be an array of strings`)
  const items: string[] = []
  for (const item of value) {
    if (typeof item !== 'string' || !item.trim()) {
      fail(label, `${field} must contain only non-empty strings`)
    }
    if (items.includes(item)) fail(label, `duplicate ${field} entry: ${item}`)
    items.push(item)
  }
  return items
}

function validateExecution(
  value: unknown,
  legacyTimeout: unknown,
  label: string,
): ToolExecutionDescriptor {
  if (value !== undefined && !isRecord(value)) {
    fail(label, 'execution must be an object')
  }
  const execution = value as Record<string, unknown> | undefined
  if (execution) {
    for (const field of Object.keys(execution)) {
      if (!EXECUTION_FIELDS.has(field)) fail(label, `unsupported execution field: ${field}`)
    }
  }

  const legacyTimeoutSeconds = validateTimeout(legacyTimeout, label)
  const timeoutSeconds = execution?.timeout_seconds === undefined
    ? legacyTimeoutSeconds
    : validateTimeout(execution.timeout_seconds, label)
  if (
    legacyTimeout !== undefined
    && execution?.timeout_seconds !== undefined
    && legacyTimeoutSeconds !== timeoutSeconds
  ) {
    fail(label, 'timeout_seconds and execution.timeout_seconds must match')
  }

  const cancellable = execution?.cancellable === undefined
    ? false
    : execution.cancellable
  if (typeof cancellable !== 'boolean') {
    fail(label, 'execution.cancellable must be a boolean')
  }
  const concurrency = execution?.concurrency === undefined
    ? 'exclusive'
    : execution.concurrency
  if (typeof concurrency !== 'string' || !TOOL_CONCURRENCY.has(concurrency)) {
    fail(label, 'execution.concurrency must be "exclusive" or "parallel"')
  }
  const effects = validateStringList(
    execution?.effects,
    label,
    'execution.effects',
    ['sandbox_read', 'sandbox_write'],
  )
  for (const effect of effects) {
    if (!TOOL_EFFECT_SET.has(effect)) {
      fail(label, `unsupported execution effect: ${effect}`)
    }
  }

  const rawCredentials = execution?.credentials ?? []
  if (!Array.isArray(rawCredentials) || rawCredentials.length > 8) fail(label, 'invalid credential requirements')
  const slots = new Set<string>()
  const credentials = rawCredentials.map((item: unknown) => {
    if (!isRecord(item) || Object.keys(item).some(key => !['slot', 'provider'].includes(key))) {
      fail(label, 'invalid credential requirement')
    }
    if (typeof item.slot !== 'string' || !/^[a-z][a-z0-9_]{0,31}$/.test(item.slot)
      || typeof item.provider !== 'string' || !/^[a-z][a-z0-9_-]{0,63}$/.test(item.provider)
      || slots.has(item.slot)) fail(label, 'invalid credential slot or provider')
    slots.add(item.slot)
    return { slot: item.slot, provider: item.provider }
  })
  if (credentials.length && !effects.includes('credential_use')) fail(label, 'credentials require credential_use effect')
  const permissions = validateStringList(execution?.permissions, label, 'execution.permissions', [])
  if (permissions.length > 32 || permissions.some(value => !/^[A-Za-z][A-Za-z0-9_.:-]{0,127}$/.test(value))) {
    fail(label, 'permissions must be bounded identifiers')
  }

  return {
    timeout_seconds: timeoutSeconds,
    cancellable,
    concurrency: concurrency as ToolExecutionDescriptor['concurrency'],
    effects: effects as ToolEffect[],
    permissions,
    credentials,
  }
}

function validatePresentation(
  value: unknown,
  label: string,
): ToolPresentationDescriptor {
  if (value === undefined) return { kind: 'auto' }
  if (!isRecord(value)) fail(label, 'presentation must be an object')
  for (const field of Object.keys(value)) {
    if (!PRESENTATION_FIELDS.has(field)) {
      fail(label, `unsupported presentation field: ${field}`)
    }
  }
  const kind = value.kind
  if (typeof kind !== 'string' || !TOOL_PRESENTATION_KINDS.has(kind)) {
    fail(label, 'presentation.kind is not supported')
  }
  const descriptor: ToolPresentationDescriptor = {
    kind: kind as ToolPresentationKind,
  }
  if (value.title !== undefined) {
    descriptor.title = requireNonEmptyString(value.title, label, 'presentation.title')
  }
  if (value.description !== undefined) {
    descriptor.description = requireNonEmptyString(
      value.description,
      label,
      'presentation.description',
    )
  }
  return descriptor
}

async function loadManifest(
  rootRealPath: string,
  directoryName: string,
  ajv: Ajv,
): Promise<ValidatedPlugin> {
  const label = `${directoryName}/manifest.json`
  const manifestPath = join(rootRealPath, directoryName, 'manifest.json')
  let manifestRealPath: string
  let raw: Uint8Array
  try {
    manifestRealPath = await realpath(manifestPath)
    const escaped = relative(rootRealPath, manifestRealPath)
    if (escaped === '..' || escaped.startsWith(`..${sep}`)) {
      fail(label, 'manifest escapes the configured tools directory')
    }
    if (!(await stat(manifestRealPath)).isFile()) {
      fail(label, 'manifest is not a regular file')
    }
    raw = await readFile(manifestRealPath)
  } catch (error) {
    if (error instanceof CatalogValidationError) throw error
    fail(label, 'manifest cannot be read')
  }

  let parsed: unknown
  try {
    parsed = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(raw))
  } catch {
    fail(label, 'manifest is not valid UTF-8 JSON')
  }
  if (!isRecord(parsed)) fail(label, 'manifest root must be an object')

  const plugin = requireNonEmptyString(parsed.plugin, label, 'plugin')
  if (!PLUGIN_NAME.test(plugin)) {
    fail(label, 'plugin may contain only letters, numbers, dot, underscore, and hyphen')
  }
  const version = requireNonEmptyString(parsed.version, label, 'version')
  if (!PLUGIN_VERSION.test(version)) {
    fail(label, 'version must be a printable ASCII release identifier')
  }
  if (!Array.isArray(parsed.tools) || parsed.tools.length === 0) {
    fail(label, 'tools must be a non-empty array')
  }

  const handler = parsed.handler === undefined
    ? 'handler.py'
    : requireNonEmptyString(parsed.handler, label, 'handler')
  const pluginRoot = join(rootRealPath, directoryName)
  const handlerPath = resolve(pluginRoot, handler)
  const lexicalEscape = relative(pluginRoot, handlerPath)
  if (lexicalEscape === '..' || lexicalEscape.startsWith(`..${sep}`)) {
    fail(label, 'handler must stay inside the plugin directory')
  }
  try {
    const handlerInfo = await lstat(handlerPath)
    if (handlerInfo.isSymbolicLink() || !handlerInfo.isFile()) {
      fail(label, 'handler must be a regular file, not a symbolic link')
    }
    const handlerRealPath = await realpath(handlerPath)
    const realEscape = relative(pluginRoot, handlerRealPath)
    if (realEscape === '..' || realEscape.startsWith(`..${sep}`)) {
      fail(label, 'handler must stay inside the plugin directory')
    }
  } catch (error) {
    if (error instanceof CatalogValidationError) throw error
    fail(label, 'handler cannot be read')
  }

  const tools: ToolDescriptor[] = parsed.tools.map((candidate, index) => {
    const toolLabel = `${label} tool[${index}]`
    if (!isRecord(candidate)) fail(toolLabel, 'tool must be an object')
    for (const field of Object.keys(candidate)) {
      if (!TOOL_FIELDS.has(field)) fail(toolLabel, `unsupported tool field: ${field}`)
    }
    if (
      candidate.contract_version !== undefined
      && candidate.contract_version !== TOOL_CONTRACT_VERSION
    ) {
      fail(toolLabel, `contract_version must be ${TOOL_CONTRACT_VERSION}`)
    }
    const name = requireNonEmptyString(candidate.name, toolLabel, 'name')
    if (!TOOL_NAME.test(name)) {
      fail(toolLabel, 'name must be a 1-64 character model-facing tool name')
    }
    if (RESERVED_AGENT_TOOL_NAMES.has(name)) {
      fail(toolLabel, `tool name is reserved by the Agent runtime: ${name}`)
    }
    const execution = validateExecution(
      candidate.execution,
      candidate.timeout_seconds,
      toolLabel,
    )
    return {
      contract_version: TOOL_CONTRACT_VERSION,
      name,
      description: requireNonEmptyString(candidate.description, toolLabel, 'description'),
      parameters: validateParameters(candidate.parameters, toolLabel, ajv),
      output_schema: validateOutputSchema(candidate.output_schema, toolLabel, ajv),
      execution,
      presentation: validatePresentation(candidate.presentation, toolLabel),
      scopes: validateScopes(candidate.scopes, toolLabel),
      timeout_seconds: execution.timeout_seconds,
      plugin,
      version,
    }
  })

  return {
    descriptor: {
      plugin,
      version,
      manifest_digest: sha256(raw),
      tool_count: tools.length,
    },
    tools,
  }
}

export interface ValidatedManifestSet {
  plugins: ValidatedPlugin[]
  manifestDigest: string
}

/** Validate the complete directory before any Cordis fiber is created. */
export async function loadValidatedManifests(
  toolsDirectory: string,
): Promise<ValidatedManifestSet> {
  // Ajv registers schema `$id` values in the validator instance. Keep one
  // instance for this complete candidate so duplicate ids inside the candidate
  // are rejected, but never retain those ids across independent reloads.
  const ajv = new Ajv({ allErrors: true, strict: false, validateFormats: false })
  let rootRealPath: string
  let entries
  try {
    rootRealPath = await realpath(toolsDirectory)
    entries = await readdir(rootRealPath, { withFileTypes: true })
  } catch {
    throw new CatalogValidationError('Tool plugin directory is unavailable')
  }

  const candidateDirectories = entries
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .sort()

  // Match the legacy tools/*/manifest.json discovery rule: unrelated folders
  // are ignored, but every manifest that does exist is validated below.
  const directories: string[] = []
  for (const directory of candidateDirectories) {
    try {
      if ((await stat(join(rootRealPath, directory, 'manifest.json'))).isFile()) {
        directories.push(directory)
      }
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code
      if (code !== 'ENOENT' && code !== 'ENOTDIR') {
        throw new CatalogValidationError(
          `${directory}/manifest.json: manifest cannot be inspected`,
        )
      }
    }
  }

  const plugins = await Promise.all(
    directories.map((directory) => loadManifest(rootRealPath, directory, ajv)),
  )

  const pluginNames = new Set<string>()
  const toolNames = new Set<string>()
  for (const item of plugins) {
    if (pluginNames.has(item.descriptor.plugin)) {
      throw new CatalogValidationError(
        `Duplicate plugin name: ${item.descriptor.plugin}`,
      )
    }
    pluginNames.add(item.descriptor.plugin)
    for (const tool of item.tools) {
      if (toolNames.has(tool.name)) {
        throw new CatalogValidationError(`Duplicate tool name: ${tool.name}`)
      }
      toolNames.add(tool.name)
    }
  }

  // Plugin names are ASCII-only. Ordering by that stable identity makes this
  // digest portable to the Python sandbox runner regardless of directory-name
  // Unicode sorting differences between JavaScript and Python.
  const digestInput = plugins
    .map(({ descriptor }) => ({
      plugin: descriptor.plugin,
      version: descriptor.version,
      manifest_digest: descriptor.manifest_digest,
    }))
    .sort((left, right) => left.plugin < right.plugin ? -1 : left.plugin > right.plugin ? 1 : 0)
  return {
    plugins,
    manifestDigest: sha256(stableStringify(digestInput)),
  }
}
