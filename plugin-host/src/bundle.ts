import { lstat, readFile, readdir, realpath } from 'node:fs/promises'
import { join } from 'node:path'

import { CatalogValidationError } from './errors.js'
import { sha256, stableStringify } from './json.js'

interface BundleRecord {
  path: string
  sha256: string
}

const CONTRACT_FILES = [
  'Dockerfile',
  'pyproject.toml',
  'supervisord.conf',
  'uv.lock',
]
const CONTRACT_DIRECTORIES = ['app', 'scientific_operators', 'scripts']
const PORTABLE_PATH = /^[\x20-\x7e]+$/

function isIgnored(relativePath: string): boolean {
  const parts = relativePath.split('/')
  const name = parts.at(-1) ?? ''
  return parts.some((part) => part === '__pycache__' || part === '.pytest_cache')
    || name === '.DS_Store'
    || name.endsWith('.pyc')
}

async function collectTree(
  root: string,
  logicalPrefix: string,
  records: BundleRecord[],
): Promise<void> {
  let rootRealPath: string
  try {
    rootRealPath = await realpath(root)
  } catch {
    throw new CatalogValidationError(`Execution bundle source is unavailable: ${logicalPrefix}`)
  }

  async function walk(directory: string, relativeDirectory = ''): Promise<void> {
    const entries = await readdir(directory, { withFileTypes: true })
    entries.sort((left, right) => left.name < right.name ? -1 : left.name > right.name ? 1 : 0)
    for (const entry of entries) {
      const relativePath = relativeDirectory
        ? `${relativeDirectory}/${entry.name}`
        : entry.name
      if (isIgnored(relativePath)) continue
      const logicalPath = `${logicalPrefix}/${relativePath}`
      if (!PORTABLE_PATH.test(logicalPath)) {
        throw new CatalogValidationError(
          `Execution bundle path is not portable: ${logicalPath}`,
        )
      }
      const absolutePath = join(directory, entry.name)
      if (entry.isSymbolicLink()) {
        throw new CatalogValidationError(
          `Execution bundle cannot contain symbolic links: ${logicalPath}`,
        )
      }
      if (entry.isDirectory()) {
        await walk(absolutePath, relativePath)
      } else if (entry.isFile()) {
        records.push({ path: logicalPath, sha256: sha256(await readFile(absolutePath)) })
      } else {
        throw new CatalogValidationError(
          `Execution bundle contains an unsupported entry: ${logicalPath}`,
        )
      }
    }
  }

  await walk(rootRealPath)
}

async function collectContract(
  root: string,
  records: BundleRecord[],
): Promise<void> {
  let rootRealPath: string
  try {
    rootRealPath = await realpath(root)
  } catch {
    throw new CatalogValidationError('Sandbox execution contract is unavailable')
  }
  for (const filename of CONTRACT_FILES) {
    const absolutePath = join(rootRealPath, filename)
    try {
      const metadata = await lstat(absolutePath)
      if (metadata.isSymbolicLink() || !metadata.isFile()) {
        throw new Error('not a regular file')
      }
      records.push({
        path: `sandbox/${filename}`,
        sha256: sha256(await readFile(absolutePath)),
      })
    } catch {
      throw new CatalogValidationError(
        `Sandbox execution contract is incomplete: ${filename}`,
      )
    }
  }
  for (const directory of CONTRACT_DIRECTORIES) {
    await collectTree(
      join(rootRealPath, directory),
      `sandbox/${directory}`,
      records,
    )
  }
}

/** Hash every source file that defines or executes a bundled analysis tool. */
export async function computeExecutionBundleDigest(
  toolsDirectory: string,
  executionContractDirectory?: string,
): Promise<string> {
  const records: BundleRecord[] = []
  await collectTree(toolsDirectory, 'tools', records)
  if (executionContractDirectory) {
    await collectContract(executionContractDirectory, records)
  }
  records.sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0)
  return sha256(stableStringify(records))
}
