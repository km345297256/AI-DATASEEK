<template>
  <div class="flex h-full w-full flex-col overflow-hidden bg-[var(--background-gray-main)]">
    <div class="mobile-safe-top flex items-start gap-3 border-b border-[var(--border-main)] px-3 pb-4 sm:block sm:px-6 sm:py-5">
      <button
        v-if="!isLeftPanelShow"
        type="button"
        class="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg hover:bg-[var(--fill-tsp-gray-main)] sm:hidden"
        :aria-label="t('Open navigation')"
        @click="toggleLeftPanel"
      >
        <PanelLeft class="size-5 text-[var(--icon-secondary)]" />
      </button>
      <div class="min-w-0 flex-1">
        <div class="text-2xl font-semibold text-[var(--text-primary)]">{{ t('Plugins') }}</div>
        <div class="mt-1 text-sm text-[var(--text-tertiary)]">{{ t('Discover official and community plugins, then add them to your personal library.') }}</div>
      </div>
    </div>

    <div class="border-b border-[var(--border-main)] bg-[var(--background-menu-white)] px-4 sm:px-6">
      <select
        :value="activeTab"
        class="my-3 h-11 w-full rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] px-3 text-sm text-[var(--text-primary)] outline-none sm:hidden"
        :aria-label="t('Plugin type')"
        @change="setActiveTab(($event.target as HTMLSelectElement).value as PluginTab)"
      >
        <option v-for="tab in tabs" :key="tab.key" :value="tab.key">{{ tab.label }}</option>
      </select>
      <div class="mx-auto hidden max-w-[1080px] gap-2 overflow-x-auto py-3 sm:flex">
        <button
          v-for="tab in tabs"
          :key="tab.key"
          type="button"
          class="shrink-0 rounded-xl px-4 py-2 text-sm transition-colors"
          :class="activeTab === tab.key ? 'bg-[var(--fill-tsp-white-main)] text-[var(--text-primary)] shadow-sm' : 'text-[var(--text-tertiary)] hover:bg-[var(--fill-tsp-white-light)]'"
          @click="setActiveTab(tab.key)"
        >
          {{ tab.label }}
        </button>
      </div>
    </div>

    <div class="flex-1 overflow-y-auto px-4 py-4 sm:px-6 sm:py-5">
      <div class="mx-auto flex max-w-[1080px] flex-col gap-4 sm:gap-6">
        <div class="rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-3 sm:p-4">
          <label class="flex items-center gap-3 rounded-xl border border-[var(--border-main)] bg-[var(--background-gray-main)] px-3 py-2">
            <Search :size="18" class="shrink-0 text-[var(--icon-secondary)]" />
            <input
              v-model="activeSearchQuery"
              class="h-8 flex-1 bg-transparent text-sm text-[var(--text-primary)] outline-none"
              :placeholder="activeSearchPlaceholder"
            />
          </label>
          <div class="mt-3 flex gap-1 overflow-x-auto" role="tablist" :aria-label="t('Publisher')">
            <button
              v-for="source in sourceFilters"
              :key="source.key"
              type="button"
              class="shrink-0 rounded-lg px-3 py-1.5 text-sm"
              :class="pluginSource === source.key ? 'bg-[var(--Button-primary-black)] text-[var(--text-onblack)]' : 'text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-white-light)]'"
              @click="pluginSource = source.key"
            >
              {{ source.label }}
            </button>
          </div>
        </div>

        <section v-if="activeTab === 'mcp'" class="rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-4 sm:p-5">
          <div class="mb-4 flex flex-col items-stretch gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <h2 class="text-lg font-semibold text-[var(--text-primary)]">{{ t('MCP Tools') }}</h2>
              <p class="mt-1 text-sm text-[var(--text-tertiary)]">{{ t('Add stdio, SSE, or streamable HTTP MCP servers') }}</p>
            </div>
            <button
              class="inline-flex h-11 w-full items-center justify-center gap-1.5 rounded-lg border border-[var(--border-btn-main)] px-3 text-sm text-[var(--text-primary)] hover:bg-[var(--fill-tsp-white-light)] sm:h-9 sm:w-auto"
              @click="startCreateMcp"
            >
              <Plus :size="15" />
              {{ t('Add') }}
            </button>
          </div>

          <div v-if="mcpEditing" ref="mcpEditorRef" class="mb-4 rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] p-4">
            <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
              <label class="flex flex-col gap-1 text-xs text-[var(--text-secondary)]">
                {{ t('Name') }}
                <input v-model="mcpForm.name" :disabled="mcpEditingExisting" class="h-9 rounded-lg border border-[var(--border-main)] bg-transparent px-2 text-sm text-[var(--text-primary)] outline-none" />
              </label>
              <label class="flex flex-col gap-1 text-xs text-[var(--text-secondary)]">
                {{ t('Transport') }}
                <select v-model="mcpForm.transport" class="h-9 rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] px-2 text-sm text-[var(--text-primary)] outline-none">
                  <option value="stdio">stdio</option>
                  <option value="sse">sse</option>
                  <option value="streamable-http">streamable-http</option>
                </select>
              </label>
              <label class="flex flex-col gap-1 text-xs text-[var(--text-secondary)] md:col-span-2">
                {{ t('Description') }}
                <textarea v-model="mcpForm.description" rows="4" class="min-h-[96px] resize-y rounded-lg border border-[var(--border-main)] bg-transparent px-2 py-2 text-sm leading-5 text-[var(--text-primary)] outline-none" />
              </label>
              <label v-if="mcpForm.transport === 'stdio'" class="flex flex-col gap-1 text-xs text-[var(--text-secondary)]">
                {{ t('Command') }}
                <input v-model="mcpForm.command" class="h-9 rounded-lg border border-[var(--border-main)] bg-transparent px-2 text-sm text-[var(--text-primary)] outline-none" />
              </label>
              <label v-if="mcpForm.transport === 'stdio'" class="flex flex-col gap-1 text-xs text-[var(--text-secondary)]">
                {{ t('Args') }}
                <input v-model="mcpArgsText" class="h-9 rounded-lg border border-[var(--border-main)] bg-transparent px-2 text-sm text-[var(--text-primary)] outline-none" />
              </label>
              <label v-if="mcpForm.transport !== 'stdio'" class="flex flex-col gap-1 text-xs text-[var(--text-secondary)] md:col-span-2">
                URL
                <input v-model="mcpForm.url" class="h-9 rounded-lg border border-[var(--border-main)] bg-transparent px-2 text-sm text-[var(--text-primary)] outline-none" />
              </label>
              <label v-if="mcpForm.transport !== 'stdio'" class="flex flex-col gap-1 text-xs text-[var(--text-secondary)] md:col-span-2">
                Headers JSON
                <textarea v-model="mcpHeadersText" class="min-h-[84px] rounded-lg border border-[var(--border-main)] bg-transparent px-2 py-2 font-mono text-xs text-[var(--text-primary)] outline-none" placeholder='{"Authorization":"Bearer token","X-API-Key":"key"}'></textarea>
              </label>
              <label class="flex items-center gap-2 text-sm text-[var(--text-secondary)] md:col-span-2">
                <input v-model="mcpForm.enabled" type="checkbox" class="h-4 w-4 accent-[var(--Button-primary-black)]" />
                {{ t('Enabled') }}
              </label>
            </div>
            <div class="mt-4 flex justify-end gap-2">
              <button class="rounded-[10px] border border-[var(--border-btn-main)] px-3 py-2 text-sm text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-white-dark)]" @click="mcpEditing = false">
                {{ t('Cancel') }}
              </button>
              <button class="rounded-[10px] bg-[var(--Button-primary-black)] px-3 py-2 text-sm text-[var(--text-onblack)] hover:opacity-90" :disabled="mcpSaving" @click="saveMcp">
                {{ mcpSaving ? t('Saving...') : t('Save') }}
              </button>
            </div>
          </div>

          <div v-if="mcpLoading" class="py-8 text-center text-sm text-[var(--text-tertiary)]">{{ t('Loading') }}...</div>
          <div v-else-if="filteredMcpServers.length === 0" class="rounded-xl border border-dashed border-[var(--border-main)] py-8 text-center text-sm text-[var(--text-tertiary)]">{{ t('No MCP servers yet') }}</div>
          <div v-else class="grid gap-3 md:grid-cols-2">
            <article v-for="server in pagedMcpServers" :key="server.name" class="min-h-[124px] rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] p-4 transition-colors hover:bg-[var(--fill-tsp-white-light)]" :class="canManageMcp(server) ? 'cursor-pointer' : ''" @click="canManageMcp(server) && startEditMcp(server)">
              <div class="flex items-start justify-between gap-3">
                <div class="min-w-0">
                  <div class="flex items-center gap-2">
                    <div class="truncate text-sm font-semibold text-[var(--text-primary)]">{{ server.name }}</div>
                    <span class="rounded border border-[var(--border-main)] px-1.5 py-0.5 text-[11px] text-[var(--text-tertiary)]">{{ server.transport }}</span>
                    <span v-if="!server.enabled" class="text-[11px] text-[var(--text-tertiary)]">{{ t('Disabled') }}</span>
                    <span class="rounded border border-[var(--border-main)] px-1.5 py-0.5 text-[11px] text-[var(--text-tertiary)]">{{ t(server.source === 'official' ? 'Official' : server.source === 'community' ? 'Community' : 'Personal') }}</span>
                  </div>
                  <div class="description-clamp mt-2 text-xs leading-5 text-[var(--text-tertiary)]">{{ server.description || server.url || server.command || t('No description') }}</div>
                </div>
                <div class="flex shrink-0 gap-1">
                  <button
                    class="flex size-8 items-center justify-center rounded-md border border-[var(--border-main)] disabled:opacity-60"
                    :disabled="pluginUpdating === `mcp:${server.name}` || isOwnedMcp(server)"
                    :title="t(server.installed && !isOwnedMcp(server) ? 'Remove from my plugins' : server.installed ? 'Installed' : 'Add to my plugins')"
                    @click.stop="toggleMcpInstallation(server)"
                  ><Check v-if="server.installed" :size="15" /><Plus v-else :size="15" /></button>
                  <template v-if="canManageMcp(server)">
                    <button class="rounded-md p-1 hover:bg-[var(--fill-tsp-white-dark)]" :title="t('Edit')" @click.stop="startEditMcp(server)"><Pencil :size="14" /></button>
                    <button class="rounded-md p-1 text-[var(--function-error)] hover:bg-[var(--fill-tsp-white-dark)]" :title="t('Delete')" @click.stop="deleteMcp(server.name)"><Trash2 :size="14" /></button>
                  </template>
                </div>
              </div>
            </article>
          </div>
          <div v-if="mcpTotalPages > 1" class="mt-4 flex items-center justify-end gap-2">
            <button class="rounded-lg border border-[var(--border-main)] px-2 py-1 text-xs text-[var(--text-secondary)] disabled:opacity-40" :disabled="mcpPage === 1" @click="mcpPage--">{{ t('Previous') }}</button>
            <span class="text-xs text-[var(--text-tertiary)]">{{ mcpPage }} / {{ mcpTotalPages }}</span>
            <button class="rounded-lg border border-[var(--border-main)] px-2 py-1 text-xs text-[var(--text-secondary)] disabled:opacity-40" :disabled="mcpPage === mcpTotalPages" @click="mcpPage++">{{ t('Next') }}</button>
          </div>
        </section>

        <section v-if="activeTab === 'skills'" class="rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-4 sm:p-5">
          <div class="mb-4 flex flex-col items-stretch gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <h2 class="text-lg font-semibold text-[var(--text-primary)]">{{ t('Skills') }}</h2>
              <p class="mt-1 text-sm text-[var(--text-tertiary)]">{{ t('Upload Markdown or ZIP skill packages') }}</p>
            </div>
            <button class="inline-flex h-11 w-full items-center justify-center gap-1.5 rounded-lg border border-[var(--border-btn-main)] px-3 text-sm text-[var(--text-primary)] hover:bg-[var(--fill-tsp-white-light)] sm:h-9 sm:w-auto" :disabled="skillUploading" @click="skillFileInput?.click()">
              <Upload :size="15" />
              {{ skillUploading ? t('Uploading...') : t('Upload') }}
            </button>
            <input ref="skillFileInput" class="hidden" type="file" accept=".md,.zip" @change="uploadSkillPackage" />
          </div>

          <div v-if="skillLoading" class="py-8 text-center text-sm text-[var(--text-tertiary)]">{{ t('Loading') }}...</div>
          <div v-else-if="filteredSkills.length === 0" class="rounded-xl border border-dashed border-[var(--border-main)] py-8 text-center text-sm text-[var(--text-tertiary)]">{{ t('No skills yet') }}</div>
          <div v-else class="grid gap-3 md:grid-cols-2">
            <article v-for="skill in pagedSkills" :key="skill.id" class="min-h-[124px] cursor-pointer rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] p-4 hover:bg-[var(--fill-tsp-white-light)]" @click="openSkill(skill.name)">
              <div class="flex items-start justify-between gap-3">
                <div class="min-w-0">
                  <div class="truncate text-sm font-semibold text-[var(--text-primary)]">{{ skill.name }}</div>
                  <div class="description-clamp mt-2 text-xs leading-5 text-[var(--text-tertiary)]">{{ skill.description || t('No description') }}</div>
                </div>
                <div class="flex shrink-0 items-center gap-1.5">
                  <span class="rounded border border-[var(--border-main)] px-1.5 py-0.5 text-[11px] text-[var(--text-tertiary)]">{{ t(skill.source === 'official' ? 'Official' : 'Personal') }}</span>
                  <select
                    v-if="canManageSkillScope(skill)"
                    :value="skill.scope"
                    class="h-8 rounded-md border border-[var(--border-main)] bg-[var(--background-menu-white)] px-2 text-xs text-[var(--text-secondary)] outline-none disabled:opacity-50"
                    :disabled="skillScopeUpdating === skill.id"
                    :aria-label="t('Skill visibility')"
                    @click.stop
                    @change.stop="changeSkillScope(skill, ($event.target as HTMLSelectElement).value as 'user' | 'global')"
                  >
                    <option value="user">{{ t('Personal') }}</option>
                    <option value="global">{{ t('Global') }}</option>
                  </select>
                  <button
                    class="flex size-8 items-center justify-center rounded-md border border-[var(--border-main)] disabled:opacity-60"
                    :disabled="pluginUpdating === `skill:${skill.id}` || canManageSkillScope(skill)"
                    :title="t(skill.installed && !canManageSkillScope(skill) ? 'Remove from my plugins' : skill.installed ? 'Installed' : 'Add to my plugins')"
                    @click.stop="toggleSkillInstallation(skill)"
                  ><Check v-if="skill.installed" :size="15" /><Plus v-else :size="15" /></button>
                </div>
              </div>
              <div v-if="skill.triggers.length" class="mt-3 flex flex-wrap gap-1">
                <span v-for="trigger in skill.triggers.slice(0, 8)" :key="trigger" class="rounded border border-[var(--border-main)] px-1.5 py-0.5 text-[11px] text-[var(--text-tertiary)]">{{ trigger }}</span>
              </div>
            </article>
          </div>
          <div v-if="skillTotalPages > 1" class="mt-4 flex items-center justify-end gap-2">
            <button class="rounded-lg border border-[var(--border-main)] px-2 py-1 text-xs text-[var(--text-secondary)] disabled:opacity-40" :disabled="skillPage === 1" @click="skillPage--">{{ t('Previous') }}</button>
            <span class="text-xs text-[var(--text-tertiary)]">{{ skillPage }} / {{ skillTotalPages }}</span>
            <button class="rounded-lg border border-[var(--border-main)] px-2 py-1 text-xs text-[var(--text-secondary)] disabled:opacity-40" :disabled="skillPage === skillTotalPages" @click="skillPage++">{{ t('Next') }}</button>
          </div>
        </section>

        <section v-if="activeTab === 'renderers'" class="rounded-xl border border-[var(--border-main)] bg-[var(--background-menu-white)] p-4 sm:p-5">
          <div class="mb-4 flex flex-col items-stretch gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <h2 class="text-lg font-semibold text-[var(--text-primary)]">{{ t('Renderers') }}</h2>
              <p class="mt-1 text-sm text-[var(--text-tertiary)]">{{ t('Renderers map file extensions to preview components or APIs.') }}</p>
            </div>
            <button
              class="inline-flex h-11 w-full items-center justify-center gap-1.5 rounded-lg border border-[var(--border-btn-main)] px-3 text-sm text-[var(--text-primary)] hover:bg-[var(--fill-tsp-white-light)] sm:h-9 sm:w-auto"
              @click="startCreateRenderer"
            >
              <Plus :size="15" />
              {{ t('Add') }}
            </button>
          </div>

          <div v-if="rendererEditing" ref="rendererEditorRef" class="mb-4 rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] p-4">
            <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
              <label class="flex flex-col gap-1 text-xs text-[var(--text-secondary)]">
                {{ t('Name') }}
                <input v-model="rendererForm.name" class="h-9 rounded-lg border border-[var(--border-main)] bg-transparent px-2 text-sm text-[var(--text-primary)] outline-none" />
              </label>
              <label class="flex flex-col gap-1 text-xs text-[var(--text-secondary)]">
                {{ t('Type') }}
                <select v-model="rendererForm.kind" class="h-9 rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] px-2 text-sm text-[var(--text-primary)] outline-none">
                  <option value="api">api</option>
                  <option value="component">component</option>
                </select>
              </label>
              <label class="flex flex-col gap-1 text-xs text-[var(--text-secondary)] md:col-span-2">
                {{ t('Description') }}
                <textarea v-model="rendererForm.description" rows="4" class="min-h-[96px] resize-y rounded-lg border border-[var(--border-main)] bg-transparent px-2 py-2 text-sm leading-5 text-[var(--text-primary)] outline-none" />
              </label>
              <label class="flex flex-col gap-1 text-xs text-[var(--text-secondary)] md:col-span-2">
                {{ t('Extensions') }}
                <input v-model="rendererExtensionsText" class="h-9 rounded-lg border border-[var(--border-main)] bg-transparent px-2 text-sm text-[var(--text-primary)] outline-none" placeholder="png, csv, html" />
              </label>
              <label v-if="rendererForm.kind === 'api'" class="flex flex-col gap-1 text-xs text-[var(--text-secondary)] md:col-span-2">
                API URL
                <input v-model="rendererForm.api_url" class="h-9 rounded-lg border border-[var(--border-main)] bg-transparent px-2 text-sm text-[var(--text-primary)] outline-none" />
              </label>
              <label v-if="rendererForm.kind === 'component'" class="flex flex-col gap-1 text-xs text-[var(--text-secondary)] md:col-span-2">
                {{ t('Entry') }}
                <input v-model="rendererForm.entry" class="h-9 rounded-lg border border-[var(--border-main)] bg-transparent px-2 text-sm text-[var(--text-primary)] outline-none" placeholder="iframe or package entry" />
              </label>
              <label class="flex items-center gap-2 text-sm text-[var(--text-secondary)] md:col-span-2">
                <input v-model="rendererForm.enabled" type="checkbox" class="h-4 w-4 accent-[var(--Button-primary-black)]" />
                {{ t('Enabled') }}
              </label>
            </div>
            <div class="mt-4 flex justify-end gap-2">
              <button class="rounded-[10px] border border-[var(--border-btn-main)] px-3 py-2 text-sm text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-white-dark)]" @click="rendererEditing = false">
                {{ t('Cancel') }}
              </button>
              <button class="rounded-[10px] bg-[var(--Button-primary-black)] px-3 py-2 text-sm text-[var(--text-onblack)] hover:opacity-90" :disabled="rendererSaving" @click="saveRenderer">
                {{ rendererSaving ? t('Saving...') : t('Save') }}
              </button>
            </div>
          </div>

          <div v-if="rendererLoading" class="py-8 text-center text-sm text-[var(--text-tertiary)]">{{ t('Loading') }}...</div>
          <div v-else-if="filteredRenderers.length === 0" class="rounded-xl border border-dashed border-[var(--border-main)] py-8 text-center text-sm text-[var(--text-tertiary)]">{{ t('No renderers found') }}</div>
          <div v-else class="grid gap-3 md:grid-cols-2">
            <article
              v-for="renderer in pagedRenderers"
              :key="renderer.id"
              class="min-h-[124px] rounded-lg border border-[var(--border-main)] bg-[var(--background-gray-main)] p-4 transition-colors"
              :class="canManageRenderer(renderer) ? 'cursor-pointer hover:bg-[var(--fill-tsp-white-light)]' : ''"
              :title="canManageRenderer(renderer) ? t('Edit') : renderer.description"
              @click="canManageRenderer(renderer) && startEditRenderer(renderer)"
            >
              <div class="flex items-start justify-between gap-3">
                <div class="min-w-0">
                  <div class="truncate text-sm font-semibold text-[var(--text-primary)]">{{ renderer.name }}</div>
                  <div class="description-clamp mt-2 text-xs leading-5 text-[var(--text-tertiary)]">{{ renderer.description || t('No description') }}</div>
                </div>
                <span class="shrink-0 rounded border border-[var(--border-main)] px-1.5 py-0.5 text-[11px] text-[var(--text-tertiary)]">{{ renderer.kind }}</span>
              </div>
              <div class="mt-3 flex flex-wrap gap-1">
                <span v-for="extension in renderer.extensions" :key="extension" class="rounded border border-[var(--border-main)] px-1.5 py-0.5 text-[11px] text-[var(--text-tertiary)]">.{{ extension }}</span>
              </div>
              <div class="mt-3 flex items-center justify-between gap-2">
                <span class="text-[11px] text-[var(--text-tertiary)]">{{ t(renderer.source === 'official' ? 'Official' : 'Personal') }}</span>
                <div class="flex shrink-0 gap-1">
                  <button
                    class="flex size-8 items-center justify-center rounded-md border border-[var(--border-main)] disabled:opacity-60"
                    :disabled="pluginUpdating === `renderer:${renderer.id}` || renderer.kind === 'builtin' || isOwnedRenderer(renderer)"
                    :title="t(renderer.installed && renderer.kind !== 'builtin' && !isOwnedRenderer(renderer) ? 'Remove from my plugins' : renderer.installed ? 'Installed' : 'Add to my plugins')"
                    @click.stop="toggleRendererInstallation(renderer)"
                  ><Check v-if="renderer.installed" :size="15" /><Plus v-else :size="15" /></button>
                  <template v-if="canManageRenderer(renderer)">
                    <button class="rounded-md p-1 hover:bg-[var(--fill-tsp-white-dark)]" :title="t('Edit')" @click.stop="startEditRenderer(renderer)"><Pencil :size="14" /></button>
                    <button class="rounded-md p-1 text-[var(--function-error)] hover:bg-[var(--fill-tsp-white-dark)]" :title="t('Delete')" @click.stop="deleteRenderer(renderer.id)"><Trash2 :size="14" /></button>
                  </template>
                </div>
              </div>
            </article>
          </div>
          <div v-if="rendererTotalPages > 1" class="mt-4 flex items-center justify-end gap-2">
            <button class="rounded-lg border border-[var(--border-main)] px-2 py-1 text-xs text-[var(--text-secondary)] disabled:opacity-40" :disabled="rendererPage === 1" @click="rendererPage--">{{ t('Previous') }}</button>
            <span class="text-xs text-[var(--text-tertiary)]">{{ rendererPage }} / {{ rendererTotalPages }}</span>
            <button class="rounded-lg border border-[var(--border-main)] px-2 py-1 text-xs text-[var(--text-secondary)] disabled:opacity-40" :disabled="rendererPage === rendererTotalPages" @click="rendererPage++">{{ t('Next') }}</button>
          </div>
        </section>
      </div>
    </div>

    <Dialog v-model:open="skillDetailOpen">
      <DialogContent class="w-[min(1100px,calc(100vw-32px))]">
        <DialogHeader>
          <DialogTitle>{{ selectedSkillDetail?.skill.name || t('Skill Detail') }}</DialogTitle>
          <p v-if="selectedSkillDetail?.skill.description" class="mt-2 whitespace-pre-wrap text-sm leading-5 text-[var(--text-tertiary)]">
            {{ selectedSkillDetail.skill.description }}
          </p>
        </DialogHeader>
        <div class="grid h-[70vh] grid-cols-[280px_1fr] overflow-hidden px-6 pb-5 pt-2">
          <div class="overflow-y-auto border-r border-[var(--border-main)] pr-3">
            <div v-for="node in selectedSkillDetail?.tree || []" :key="node.path">
              <SkillTreeNode :node="node" :active-path="selectedSkillFilePath" @select="selectSkillFile" />
            </div>
          </div>
          <div class="min-w-0 overflow-y-auto pl-4">
            <div v-if="selectedSkillFile" class="h-full">
              <div class="mb-3 flex items-center justify-between gap-3">
                <div class="min-w-0 truncate text-xs text-[var(--text-tertiary)]">{{ selectedSkillFile.path }}</div>
                <div class="flex shrink-0 items-center gap-2">
                  <button
                    v-if="!editingSkillFile"
                    class="rounded-lg border border-[var(--border-main)] px-2 py-1 text-xs text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-white-light)]"
                    :disabled="selectedSkillFile.binary"
                    @click="startEditSkillFile"
                  >
                    {{ t('Edit') }}
                  </button>
                  <template v-else>
                    <button class="rounded-lg border border-[var(--border-main)] px-2 py-1 text-xs text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-white-light)]" @click="cancelEditSkillFile">{{ t('Cancel') }}</button>
                    <button class="rounded-lg bg-[var(--Button-primary-black)] px-2 py-1 text-xs text-[var(--text-onblack)] disabled:opacity-50" :disabled="savingSkillFile" @click="saveSkillFile">{{ savingSkillFile ? t('Saving...') : t('Save') }}</button>
                  </template>
                </div>
              </div>
              <textarea
                v-if="editingSkillFile"
                v-model="skillFileDraft"
                class="h-[calc(70vh-92px)] w-full resize-none rounded-xl border border-[var(--border-main)] bg-[var(--background-gray-main)] p-4 font-mono text-sm leading-6 text-[var(--text-primary)] outline-none"
                spellcheck="false"
              />
              <div
                v-else-if="isMarkdownFile(selectedSkillFile.path)"
                class="prose prose-sm max-w-none rounded-xl border border-[var(--border-main)] bg-[var(--background-gray-main)] p-4 text-[var(--text-primary)] dark:prose-invert"
                v-html="renderMarkdown(selectedSkillFile.content)"
              >
              </div>
              <pre v-else class="min-h-full overflow-x-auto rounded-xl border border-[var(--border-main)] bg-[var(--background-gray-main)] p-4 text-sm leading-6 text-[var(--text-primary)]"><code>{{ selectedSkillFile.content }}</code></pre>
            </div>
            <div v-else class="flex h-full items-center justify-center text-sm text-[var(--text-tertiary)]">{{ t('Select a file to preview') }}</div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, defineComponent, h, nextTick, onMounted, reactive, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { useRoute, useRouter } from 'vue-router';
import { Check, Plus, Pencil, Trash2, Upload, Folder, FileText, Search, PanelLeft } from 'lucide-vue-next';
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { deleteMCPServer, installMCPServer, listMCPCatalog, saveMCPServer, uninstallMCPServer, type MCPServerInfo, type MCPTransport } from '@/api/mcp';
import { getSkillDetail, installSkill, listSkillCatalog, uninstallSkill, updateSkillFile, updateSkillScope, uploadSkill, type SkillDetailResponse, type SkillFileContent, type SkillFileNode, type SkillInfo } from '@/api/skill';
import { createRendererConfig, deleteRendererConfig, installRenderer, listRendererCatalog, uninstallRenderer, updateRendererConfig, type RendererInfo, type RendererRequest } from '@/api/renderer';
import { listBuiltinRenderers, listRenderers, rendererDefinitionsFromConfigs, type RendererDefinition } from '@/renderers/registry';
import { showErrorToast, showSuccessToast } from '@/utils/toast';
import { useLeftPanel } from '@/composables/useLeftPanel';
import { useAuth } from '@/composables/useAuth';

const { t } = useI18n();
const route = useRoute();
const router = useRouter();
const { isLeftPanelShow, toggleLeftPanel } = useLeftPanel();
const { currentUser } = useAuth();
const PAGE_SIZE = 6;

type PluginTab = 'skills' | 'mcp' | 'renderers';
const tabs = computed<Array<{ key: PluginTab; label: string }>>(() => [
  { key: 'skills', label: t('Skills') },
  { key: 'mcp', label: t('MCP') },
  { key: 'renderers', label: t('Renderers') },
]);
const tabKeys = new Set<PluginTab>(['skills', 'mcp', 'renderers']);
const routeTab = typeof route.query.tab === 'string' && tabKeys.has(route.query.tab as PluginTab)
  ? route.query.tab as PluginTab
  : 'skills';
const activeTab = ref<PluginTab>(routeTab);
type PluginSource = 'all' | 'official' | 'personal' | 'community';
const pluginSource = ref<PluginSource>('all');
const sourceFilters = computed<Array<{ key: PluginSource; label: string }>>(() => {
  const filters: Array<{ key: PluginSource; label: string }> = [
    { key: 'all', label: t('All') },
    { key: 'official', label: t('Official') },
    { key: 'personal', label: t('Personal') },
  ];
  if (activeTab.value === 'mcp' && currentUser.value?.role === 'admin') {
    filters.push({ key: 'community', label: t('Community') });
  }
  return filters;
});
const pluginUpdating = ref<string | null>(null);
const skillSearchQuery = ref('');
const mcpSearchQuery = ref('');
const rendererSearchQuery = ref('');
const activeSearchQuery = computed({
  get: () => ({
    skills: skillSearchQuery.value,
    mcp: mcpSearchQuery.value,
    renderers: rendererSearchQuery.value,
  })[activeTab.value],
  set: (value: string) => {
    if (activeTab.value === 'skills') skillSearchQuery.value = value;
    else if (activeTab.value === 'mcp') mcpSearchQuery.value = value;
    else rendererSearchQuery.value = value;
  },
});
const activeSearchPlaceholder = computed(() => ({
  skills: t('Search skills'),
  mcp: t('Search MCP servers'),
  renderers: t('Search renderers'),
})[activeTab.value]);

const setActiveTab = (tab: PluginTab) => {
  activeTab.value = tab;
  router.replace({ query: { ...route.query, tab } });
};

const mcpServers = ref<MCPServerInfo[]>([]);
const mcpLoading = ref(false);
const mcpSaving = ref(false);
const mcpEditing = ref(false);
const mcpEditingExisting = ref(false);
const mcpArgsText = ref('');
const mcpHeadersText = ref('');
const mcpEditorRef = ref<HTMLElement>();
const mcpPage = ref(1);
const mcpForm = reactive<MCPServerInfo>({
  name: '',
  transport: 'stdio',
  enabled: true,
  description: '',
  command: '',
  args: [],
  url: '',
});

const skills = ref<SkillInfo[]>([]);
const skillLoading = ref(false);
const skillUploading = ref(false);
const skillFileInput = ref<HTMLInputElement>();
const skillDetailOpen = ref(false);
const selectedSkillDetail = ref<SkillDetailResponse | null>(null);
const selectedSkillFilePath = ref('');
const skillPage = ref(1);
const editingSkillFile = ref(false);
const savingSkillFile = ref(false);
const skillFileDraft = ref('');
const skillScopeUpdating = ref<string | null>(null);
const rendererConfigs = ref<RendererInfo[]>([]);
const renderers = ref<RendererDefinition[]>(listRenderers());
const rendererPage = ref(1);
const rendererEditing = ref(false);
const rendererLoading = ref(false);
const rendererSaving = ref(false);
const rendererEditingId = ref<string | null>(null);
const rendererEditorRef = ref<HTMLElement>();
const rendererExtensionsText = ref('');
const rendererForm = reactive<RendererRequest>({
  name: '',
  description: '',
  kind: 'api',
  extensions: [],
  enabled: true,
  api_url: '',
  entry: '',
  config: {},
  is_global: false,
});

const normalizedMcpSearch = computed(() => mcpSearchQuery.value.trim().toLowerCase());
const normalizedSkillSearch = computed(() => skillSearchQuery.value.trim().toLowerCase());
const normalizedRendererSearch = computed(() => rendererSearchQuery.value.trim().toLowerCase());

const filteredMcpServers = computed(() => {
  const query = normalizedMcpSearch.value;
  return mcpServers.value.filter((server) => (pluginSource.value === 'all' || server.source === pluginSource.value) && (!query || [
    server.name,
    server.description,
    server.transport,
    server.command,
    server.url,
  ].some((value) => String(value || '').toLowerCase().includes(query))));
});

const filteredSkills = computed(() => {
  const query = normalizedSkillSearch.value;
  return skills.value.filter((skill) => (pluginSource.value === 'all' || skill.source === pluginSource.value) && (!query || [
    skill.name,
    skill.description,
    skill.scope,
    ...(skill.triggers || []),
  ].some((value) => String(value || '').toLowerCase().includes(query))));
});

const filteredRenderers = computed(() => {
  const query = normalizedRendererSearch.value;
  return renderers.value.filter((renderer) => (pluginSource.value === 'all' || renderer.source === pluginSource.value) && (!query || [
    renderer.name,
    renderer.description,
    renderer.kind,
    ...(renderer.extensions || []),
  ].some((value) => String(value || '').toLowerCase().includes(query))));
});

const mcpTotalPages = computed(() => Math.max(1, Math.ceil(filteredMcpServers.value.length / PAGE_SIZE)));
const skillTotalPages = computed(() => Math.max(1, Math.ceil(filteredSkills.value.length / PAGE_SIZE)));
const rendererTotalPages = computed(() => Math.max(1, Math.ceil(filteredRenderers.value.length / PAGE_SIZE)));
const pagedMcpServers = computed(() => filteredMcpServers.value.slice((mcpPage.value - 1) * PAGE_SIZE, mcpPage.value * PAGE_SIZE));
const pagedSkills = computed(() => filteredSkills.value.slice((skillPage.value - 1) * PAGE_SIZE, skillPage.value * PAGE_SIZE));
const pagedRenderers = computed(() => filteredRenderers.value.slice((rendererPage.value - 1) * PAGE_SIZE, rendererPage.value * PAGE_SIZE));

const selectedSkillFile = computed<SkillFileContent | null>(() => {
  if (!selectedSkillDetail.value || !selectedSkillFilePath.value) return null;
  return selectedSkillDetail.value.files.find((file) => file.path === selectedSkillFilePath.value) || null;
});

const resetMcpForm = () => {
  mcpForm.name = '';
  mcpForm.transport = 'stdio';
  mcpForm.enabled = true;
  mcpForm.description = '';
  mcpForm.command = '';
  mcpForm.args = [];
  mcpForm.url = '';
  mcpForm.headers = undefined;
  mcpForm.env = undefined;
  mcpArgsText.value = '';
  mcpHeadersText.value = '';
};

const loadMcpServers = async () => {
  mcpLoading.value = true;
  try {
    mcpServers.value = await listMCPCatalog();
  } catch (error) {
    console.error('Failed to load MCP servers:', error);
    showErrorToast(t('Failed to load MCP servers'));
  } finally {
    mcpLoading.value = false;
  }
};

const isOwnedMcp = (server: MCPServerInfo) => (server.owner_user_id || server.user_id) === currentUser.value?.id;
const canManageMcp = (server: MCPServerInfo) => isOwnedMcp(server);
const toggleMcpInstallation = async (server: MCPServerInfo) => {
  if (isOwnedMcp(server)) return;
  const key = `mcp:${server.name}`;
  pluginUpdating.value = key;
  try {
    const updated = server.installed
      ? await uninstallMCPServer(server.name)
      : await installMCPServer(server.name);
    Object.assign(server, updated);
    showSuccessToast(t(updated.installed ? 'Plugin added' : 'Plugin removed'));
  } catch (error) {
    console.error('Failed to update MCP installation:', error);
    showErrorToast(t('Failed to update plugin'));
  } finally {
    pluginUpdating.value = null;
  }
};

const loadRenderers = async () => {
  rendererLoading.value = true;
  try {
    rendererConfigs.value = await listRendererCatalog();
    renderers.value = [...listBuiltinRenderers(), ...rendererDefinitionsFromConfigs(rendererConfigs.value)];
  } catch (error) {
    console.error('Failed to load renderers:', error);
    showErrorToast(t('Failed to load renderers'));
  } finally {
    rendererLoading.value = false;
  }
};

const isOwnedRenderer = (renderer: RendererDefinition) => (renderer.owner_user_id || renderer.user_id) === currentUser.value?.id;
const canManageRenderer = (renderer: RendererDefinition) => isOwnedRenderer(renderer);
const toggleRendererInstallation = async (renderer: RendererDefinition) => {
  if (renderer.kind === 'builtin' || isOwnedRenderer(renderer)) return;
  const key = `renderer:${renderer.id}`;
  pluginUpdating.value = key;
  try {
    const updated = renderer.installed
      ? await uninstallRenderer(renderer.id)
      : await installRenderer(renderer.id);
    Object.assign(renderer, updated);
    showSuccessToast(t(updated.installed ? 'Plugin added' : 'Plugin removed'));
  } catch (error) {
    console.error('Failed to update renderer installation:', error);
    showErrorToast(t('Failed to update plugin'));
  } finally {
    pluginUpdating.value = null;
  }
};

const startCreateMcp = () => {
  resetMcpForm();
  mcpEditingExisting.value = false;
  mcpEditing.value = true;
  nextTick(() => mcpEditorRef.value?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
};

const startEditMcp = (server: MCPServerInfo) => {
  mcpForm.name = server.name;
  mcpForm.transport = server.transport;
  mcpForm.enabled = server.enabled;
  mcpForm.description = server.description || '';
  mcpForm.command = server.command || '';
  mcpForm.args = server.args || [];
  mcpForm.url = server.url || '';
  mcpForm.headers = server.headers;
  mcpForm.env = server.env;
  mcpArgsText.value = (server.args || []).join(' ');
  mcpHeadersText.value = server.headers ? JSON.stringify(server.headers, null, 2) : '';
  mcpEditingExisting.value = true;
  mcpEditing.value = true;
  nextTick(() => mcpEditorRef.value?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
};

const parseArgs = () => mcpArgsText.value.split(/\s+/).map((arg) => arg.trim()).filter(Boolean);
const parseStringMap = (value: string, label: string): Record<string, string> | undefined => {
  if (!value.trim()) return undefined;
  const parsed = JSON.parse(value);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error(`${label} must be a JSON object`);
  }
  for (const [key, item] of Object.entries(parsed)) {
    if (typeof item !== 'string') {
      throw new Error(`${label}.${key} must be a string`);
    }
  }
  return parsed as Record<string, string>;
};

const saveMcp = async () => {
  if (!mcpForm.name.trim()) {
    showErrorToast(t('Name is required'));
    return;
  }
  mcpSaving.value = true;
  try {
    const headers = mcpForm.transport !== 'stdio' ? parseStringMap(mcpHeadersText.value, 'headers') : undefined;
    await saveMCPServer({
      name: mcpForm.name.trim(),
      transport: mcpForm.transport as MCPTransport,
      enabled: mcpForm.enabled,
      description: mcpForm.description || undefined,
      command: mcpForm.transport === 'stdio' ? mcpForm.command : undefined,
      args: mcpForm.transport === 'stdio' ? parseArgs() : undefined,
      url: mcpForm.transport !== 'stdio' ? mcpForm.url : undefined,
      headers,
      env: mcpForm.env,
    });
    await loadMcpServers();
    mcpEditing.value = false;
    showSuccessToast(t('MCP server saved'));
  } catch (error) {
    console.error('Failed to save MCP server:', error);
    showErrorToast(t('Failed to save MCP server'));
  } finally {
    mcpSaving.value = false;
  }
};

const deleteMcp = async (name: string) => {
  try {
    mcpServers.value = await deleteMCPServer(name);
    showSuccessToast(t('MCP server deleted'));
  } catch (error) {
    console.error('Failed to delete MCP server:', error);
    showErrorToast(t('Failed to delete MCP server'));
  }
};

const resetRendererForm = () => {
  rendererForm.name = '';
  rendererForm.description = '';
  rendererForm.kind = 'api';
  rendererForm.extensions = [];
  rendererForm.enabled = true;
  rendererForm.api_url = '';
  rendererForm.entry = '';
  rendererForm.config = {};
  rendererForm.is_global = false;
  rendererExtensionsText.value = '';
};

const parseRendererExtensions = () => rendererExtensionsText.value
  .split(/[\s,，]+/)
  .map((extension) => extension.trim().toLowerCase().replace(/^\./, ''))
  .filter(Boolean);

const startCreateRenderer = () => {
  resetRendererForm();
  rendererEditingId.value = null;
  rendererEditing.value = true;
  nextTick(() => rendererEditorRef.value?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
};

const startEditRenderer = (renderer: RendererDefinition) => {
  if (!renderer.editable) return;
  rendererEditingId.value = renderer.id;
  rendererForm.name = renderer.name;
  rendererForm.description = renderer.description;
  rendererForm.kind = renderer.kind === 'builtin' ? 'api' : renderer.kind;
  rendererForm.extensions = [...renderer.extensions];
  rendererForm.enabled = renderer.enabled;
  rendererForm.api_url = renderer.api_url || '';
  rendererForm.entry = renderer.entry || '';
  rendererForm.config = renderer.config || {};
  rendererForm.is_global = renderer.scope === 'global';
  rendererExtensionsText.value = renderer.extensions.join(', ');
  rendererEditing.value = true;
  nextTick(() => rendererEditorRef.value?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
};

const saveRenderer = async () => {
  if (!rendererForm.name.trim()) {
    showErrorToast(t('Name is required'));
    return;
  }
  if (parseRendererExtensions().length === 0) {
    showErrorToast(t('Extensions are required'));
    return;
  }
  const payload: RendererRequest = {
    ...rendererForm,
    name: rendererForm.name.trim(),
    extensions: parseRendererExtensions(),
    api_url: rendererForm.kind === 'api' ? rendererForm.api_url || null : null,
    entry: rendererForm.kind === 'component' ? rendererForm.entry || null : null,
  };
  rendererSaving.value = true;
  try {
    if (rendererEditingId.value) {
      await updateRendererConfig(rendererEditingId.value, payload);
      showSuccessToast(t('Renderer saved'));
    } else {
      await createRendererConfig(payload);
      showSuccessToast(t('Renderer saved'));
    }
    rendererEditing.value = false;
    await loadRenderers();
  } catch (error) {
    console.error('Failed to save renderer:', error);
    showErrorToast(t('Failed to save renderer'));
  } finally {
    rendererSaving.value = false;
  }
};

const deleteRenderer = async (id: string) => {
  try {
    await deleteRendererConfig(id);
    await loadRenderers();
    showSuccessToast(t('Renderer deleted'));
  } catch (error) {
    console.error('Failed to delete renderer:', error);
    showErrorToast(t('Failed to delete renderer'));
  }
};

const loadSkills = async () => {
  skillLoading.value = true;
  try {
    skills.value = await listSkillCatalog();
  } catch (error) {
    console.error('Failed to load skills:', error);
    showErrorToast(t('Failed to load skills'));
  } finally {
    skillLoading.value = false;
  }
};

const canManageSkillScope = (skill: SkillInfo) => {
  const ownerId = skill.owner_user_id || skill.user_id;
  return Boolean(currentUser.value?.id && ownerId === currentUser.value.id);
};

const toggleSkillInstallation = async (skill: SkillInfo) => {
  if (canManageSkillScope(skill)) return;
  const key = `skill:${skill.id}`;
  pluginUpdating.value = key;
  try {
    const updated = skill.installed
      ? await uninstallSkill(skill.id)
      : await installSkill(skill.id);
    Object.assign(skill, updated);
    showSuccessToast(t(updated.installed ? 'Plugin added' : 'Plugin removed'));
  } catch (error) {
    console.error('Failed to update skill installation:', error);
    showErrorToast(t('Failed to update plugin'));
  } finally {
    pluginUpdating.value = null;
  }
};

const changeSkillScope = async (skill: SkillInfo, scope: 'user' | 'global') => {
  if (!canManageSkillScope(skill) || skill.scope === scope) return;
  const previousScope = skill.scope;
  skillScopeUpdating.value = skill.id;
  try {
    const updated = await updateSkillScope(skill.id, scope);
    Object.assign(skill, updated);
    if (selectedSkillDetail.value?.skill.id === skill.id) {
      selectedSkillDetail.value.skill = updated;
    }
    showSuccessToast(t('Skill visibility updated'));
  } catch (error) {
    skill.scope = previousScope;
    console.error('Failed to update skill visibility:', error);
    showErrorToast(t('Failed to update skill visibility'));
    await loadSkills();
  } finally {
    skillScopeUpdating.value = null;
  }
};

const uploadSkillPackage = async (event: Event) => {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  skillUploading.value = true;
  try {
    await uploadSkill(file);
    await loadSkills();
    showSuccessToast(t('Skill uploaded'));
  } catch (error) {
    console.error('Failed to upload skill:', error);
    showErrorToast(t('Failed to upload skill'));
  } finally {
    skillUploading.value = false;
    input.value = '';
  }
};

const firstFilePath = (nodes: SkillFileNode[]): string => {
  for (const node of nodes) {
    if (node.type === 'file') return node.path;
    const childPath = firstFilePath(node.children || []);
    if (childPath) return childPath;
  }
  return '';
};

const openSkill = async (name: string) => {
  try {
    selectedSkillDetail.value = await getSkillDetail(name);
    selectedSkillFilePath.value = firstFilePath(selectedSkillDetail.value.tree);
    editingSkillFile.value = false;
    skillFileDraft.value = '';
    skillDetailOpen.value = true;
  } catch (error) {
    console.error('Failed to load skill detail:', error);
    showErrorToast(t('Failed to load skill detail'));
  }
};

const selectSkillFile = (path: string) => {
  selectedSkillFilePath.value = path;
  editingSkillFile.value = false;
  skillFileDraft.value = '';
};

const startEditSkillFile = () => {
  if (!selectedSkillFile.value || selectedSkillFile.value.binary) return;
  skillFileDraft.value = selectedSkillFile.value.content;
  editingSkillFile.value = true;
};

const cancelEditSkillFile = () => {
  editingSkillFile.value = false;
  skillFileDraft.value = '';
};

const saveSkillFile = async () => {
  if (!selectedSkillDetail.value || !selectedSkillFile.value) return;
  savingSkillFile.value = true;
  try {
    const path = selectedSkillFile.value.path;
    selectedSkillDetail.value = await updateSkillFile(selectedSkillDetail.value.skill.name, path, skillFileDraft.value);
    selectedSkillFilePath.value = path;
    editingSkillFile.value = false;
    skillFileDraft.value = '';
    await loadSkills();
    showSuccessToast(t('Skill file saved'));
  } catch (error) {
    console.error('Failed to save skill file:', error);
    showErrorToast(t('Failed to save skill file'));
  } finally {
    savingSkillFile.value = false;
  }
};

const isMarkdownFile = (path: string) => path.toLowerCase().endsWith('.md') || path.toLowerCase().endsWith('.markdown');

marked.setOptions({
  breaks: true,
  gfm: true,
});

const renderMarkdown = (content: string) => DOMPurify.sanitize(marked.parse(content || '') as string);

const SkillTreeNode = defineComponent({
  name: 'SkillTreeNode',
  props: {
    node: { type: Object as () => SkillFileNode, required: true },
    activePath: { type: String, default: '' },
  },
  emits: ['select'],
  setup(props, { emit }) {
    const renderNode = (node: SkillFileNode, depth = 0): any => {
      const isFile = node.type === 'file';
      return h('div', [
        h(
          'button',
          {
            class: [
              'flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm',
              isFile ? 'cursor-pointer hover:bg-[var(--fill-tsp-white-light)]' : 'cursor-default',
              props.activePath === node.path ? 'bg-[var(--fill-tsp-white-main)] text-[var(--text-primary)]' : 'text-[var(--text-secondary)]',
            ],
            style: { paddingLeft: `${8 + depth * 14}px` },
            onClick: () => {
              if (isFile) emit('select', node.path);
            },
          },
          [
            h(isFile ? FileText : Folder, { size: 14, class: 'shrink-0 text-[var(--icon-secondary)]' }),
            h('span', { class: 'truncate' }, node.name),
          ],
        ),
        ...(node.children || []).map((child) => renderNode(child, depth + 1)),
      ]);
    };
    return () => renderNode(props.node);
  },
});

watch(mcpSearchQuery, () => {
  mcpPage.value = 1;
});

watch(skillSearchQuery, () => {
  skillPage.value = 1;
});

watch(rendererSearchQuery, () => {
  rendererPage.value = 1;
});

watch(pluginSource, () => {
  mcpPage.value = 1;
  skillPage.value = 1;
  rendererPage.value = 1;
});

watch(activeTab, (tab) => {
  if (tab !== 'mcp' && pluginSource.value === 'community') {
    pluginSource.value = 'all';
  }
});

watch(() => route.query.tab, (tab) => {
  if (typeof tab === 'string' && tabKeys.has(tab as PluginTab) && tab !== activeTab.value) {
    activeTab.value = tab as PluginTab;
  }
});

watch(filteredMcpServers, () => {
  if (mcpPage.value > mcpTotalPages.value) mcpPage.value = mcpTotalPages.value;
});

watch(filteredSkills, () => {
  if (skillPage.value > skillTotalPages.value) skillPage.value = skillTotalPages.value;
});

watch(filteredRenderers, () => {
  if (rendererPage.value > rendererTotalPages.value) rendererPage.value = rendererTotalPages.value;
});

onMounted(() => {
  loadMcpServers();
  loadSkills();
  loadRenderers();
});
</script>

<style scoped>
.description-clamp {
  display: -webkit-box;
  overflow: hidden;
  overflow-wrap: anywhere;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}
</style>
