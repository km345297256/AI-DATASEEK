import type { SkillInfo } from '@/api/skill';

// Display only: callers must keep the original selector for submission/removal.
export function skillLabel(selector: string, catalog: SkillInfo[]): string {
  const [name, digest] = selector.split('#sha256=');
  const release = catalog.find(skill => skill.name === name &&
    (!digest || skill.package_sha256 === digest));
  const match = name.match(/^(.*)@([^@]+)$/);
  const title = release?.title || match?.[1] || name;
  const version = release?.version || match?.[2];
  return version ? `${title} · v${version}` : title;
}
