import type { SkillInfo } from '@/api/skill';

export const skillCategories = {
  discovery: 'Data discovery',
  acquisition: 'Data acquisition',
  quality: 'Data quality',
  transformation: 'Data transformation',
  analysis: 'Data analysis',
  reporting: 'Results reporting',
  unclassified: 'Unclassified skills',
} as const;

export function skillCategory(skill: SkillInfo): keyof typeof skillCategories {
  return skill.format === 'data-skill-v1' && skill.category &&
    Object.prototype.hasOwnProperty.call(skillCategories, skill.category)
    ? skill.category as keyof typeof skillCategories : 'unclassified';
}

export function filterSkillCatalog(skills: SkillInfo[], filters: {
  query: string; source: string; category: string; domain: string; installed: boolean;
}): SkillInfo[] {
  const query = filters.query.trim().toLocaleLowerCase();
  return skills.filter(skill =>
    (filters.source === 'all' || skill.source === filters.source) &&
    (!filters.category || skillCategory(skill) === filters.category) &&
    (!filters.domain || (skill.domains || []).includes(filters.domain)) &&
    (!filters.installed || skill.installed) &&
    (!query || [skill.name, skill.title, skill.package_id, skill.version,
      skill.description, skill.scope, ...(skill.triggers || []), ...(skill.domains || [])]
      .some(value => value?.toLocaleLowerCase().includes(query))));
}
