<template>
  <section class="page" data-module="apigen">
    <header class="page-head">
      <div>
        <h2>API接口生成管理</h2>
        <p class="page-desc">维护API接口的方法定义、参数配置与响应模板，并把参数定义与响应模板合成为版本化的请求响应契约，供沙箱测试与文档自动生成共用。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出API接口生成清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ formatCell(row[column]) }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link primary-link" type="button" @click="openWorkbench(row)">契约工作台</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无API接口生成数据，可先登记接口配置</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条API接口生成记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <!-- 契约工作台抽屉 -->
    <div v-if="workbenchOpen" class="drawer-mask" @click.self="closeWorkbench">
      <div class="drawer">
        <header class="drawer-head">
          <div>
            <h3>请求响应契约工作台</h3>
            <p class="page-desc">{{ activeRow?.['接口编号'] }} · {{ activeRow?.['接口名称'] }} · {{ activeRow?.['请求方法'] }} {{ activeRow?.['请求路径'] }}</p>
          </div>
          <button class="btn ghost" type="button" @click="closeWorkbench">关闭</button>
        </header>

        <div class="drawer-body">
          <!-- 发布规则与适用范围 -->
          <section class="panel">
            <h4>发布规则与判定范围（保存前全部明确）</h4>
            <div v-for="rule in rules" :key="rule.group" class="rule-card">
              <strong>[{{ rule.group }}] {{ rule.name }}</strong>
              <p>{{ rule.scope }}</p>
            </div>
          </section>

          <!-- 保存前校验报告 -->
          <section class="panel">
            <div class="panel-head">
              <h4>保存前校验报告</h4>
              <div class="panel-actions">
                <button class="btn" type="button" :disabled="loading" @click="loadValidation">重新校验</button>
                <button class="btn primary" type="button" :disabled="loading" @click="publishContract">发布契约（不满足标准不允许保存）</button>
              </div>
            </div>
            <div v-if="!validation" class="empty-state">点击“重新校验”查看 A/B/C 三组判定结果</div>
            <template v-else>
              <p :class="validation.passed ? 'pass-text' : 'fail-text'">
                总体结论：{{ validation.passed ? '全部判定通过，可以发布' : '存在未通过项，禁止保存' }}
              </p>
              <div v-for="group in validation.groups" :key="group.group" class="check-group">
                <strong>[{{ group.group }}] {{ group.name }}</strong>
                <ul>
                  <li v-for="check in group.checks" :key="check.id" :class="check.passed ? 'pass-text' : 'fail-text'">
                    <span class="check-badge">{{ check.passed ? '通过' : '未通过' }}</span>
                    {{ check.name }} — {{ check.message }}
                  </li>
                </ul>
              </div>
              <h5>冻结参数预览（重名已按唯一优先级收敛）</h5>
              <table class="data-table inner-table">
                <thead>
                  <tr><th>参数名</th><th>位置</th><th>类型</th><th>必填</th><th>归一化名称</th><th>说明</th></tr>
                </thead>
                <tbody>
                  <tr v-for="param in frozenParams" :key="String(param['归一化参数名']) + String(param['参数位置'])">
                    <td>{{ param['参数名'] }}</td>
                    <td>{{ param['参数位置'] }}</td>
                    <td>{{ param['类型'] }}</td>
                    <td>{{ param['必填'] ? '是' : '否' }}</td>
                    <td>{{ param['归一化参数名'] }}</td>
                    <td>{{ param['说明'] }}</td>
                  </tr>
                </tbody>
              </table>
            </template>
          </section>

          <!-- 模板修订（只新增版本） -->
          <section class="panel">
            <div class="panel-head">
              <h4>模板修订（只影响下一次发布，已发布契约保持不动）</h4>
              <div class="panel-actions">
                <button class="btn" type="button" @click="syncDraftFromRow">载入当前配置</button>
                <button class="btn primary" type="button" :disabled="loading" @click="saveDefinitions">保存修订</button>
              </div>
            </div>
            <p class="hint">修订参数说明或响应模板后回到上方重新校验并发布，系统只新增契约版本，旧版本继续可查。</p>
            <label class="editor-label">参数定义（JSON）</label>
            <textarea v-model="draftParams" class="code-editor" rows="8"></textarea>
            <label class="editor-label">响应模板（JSON）</label>
            <textarea v-model="draftTemplate" class="code-editor" rows="10"></textarea>
          </section>

          <!-- 契约版本清单与消费方 -->
          <section class="panel">
            <div class="panel-head">
              <h4>契约版本清单（旧契约继续可查、可复现原调用）</h4>
              <button class="btn" type="button" @click="loadVersions">刷新版本</button>
            </div>
            <table class="data-table inner-table">
              <thead>
                <tr><th>契约编号</th><th>版本</th><th>状态</th><th>发布日期</th><th>冻结参数数</th><th>沙箱测试</th><th>文档生成</th></tr>
              </thead>
              <tbody>
                <tr v-for="version in versions" :key="version.id">
                  <td>{{ version['契约编号'] }}</td>
                  <td>{{ version['版本号'] }}</td>
                  <td>
                    <span :class="version['版本状态'] === '当前版本' ? 'pass-text' : 'muted-text'">{{ version['版本状态'] }}</span>
                  </td>
                  <td>{{ version['发布日期'] }}</td>
                  <td>{{ version['冻结参数'].length }}</td>
                  <td>
                    <button class="link" type="button" @click="runSandbox(version)">按冻结参数构造请求</button>
                  </td>
                  <td>
                    <button class="link" type="button" @click="runDocgen(version)">生成同版本文档</button>
                  </td>
                </tr>
                <tr v-if="!versions.length">
                  <td colspan="7" class="empty-state">尚未发布任何契约版本</td>
                </tr>
              </tbody>
            </table>
          </section>

          <!-- 消费结果 -->
          <section v-if="sandboxResult || docgenResult" class="panel">
            <h4>消费结果（沙箱与文档取自同一冻结契约）</h4>
            <div v-if="sandboxResult" class="result-box">
              <h5>沙箱实例 {{ sandboxResult.sandbox['实例编号'] }} · 契约 {{ sandboxResult.sandbox['契约编号'] }} {{ sandboxResult.sandbox['契约版本'] }}</h5>
              <p><strong>请求：</strong>{{ sandboxResult.request.method }} {{ sandboxResult.request.url }}</p>
              <p><strong>请求头：</strong>{{ JSON.stringify(sandboxResult.request.headers) }}</p>
              <p><strong>可选错误码：</strong>{{ sandboxResult.request['可选错误码'].join('、') }}</p>
              <p><strong>预期响应：</strong>{{ JSON.stringify(sandboxResult.request.expected) }}</p>
            </div>
            <div v-if="docgenResult" class="result-box">
              <h5>文档 {{ docgenResult.docgen['模板编号'] }} · 契约 {{ docgenResult.docgen['契约编号'] }} {{ docgenResult.docgen['契约版本'] }}</h5>
              <p><strong>请求参数表：</strong>{{ docgenResult.fields['请求参数表'].map((f: ParamRow) => `${f['参数名']}(${f['参数位置']})`).join('、') }}</p>
              <p><strong>响应字段表：</strong>{{ docgenResult.fields['响应字段表'].map((f: FieldRow) => f['名称']).join('、') }}</p>
              <p><strong>错误码表：</strong>{{ docgenResult.fields['错误码表'].map((f: ErrorRow) => f['错误码']).join('、') }}</p>
            </div>
          </section>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null | object>
type ParamRow = Record<string, string | number | boolean | null>
type FieldRow = Record<string, string>
type ErrorRow = Record<string, string>
type Check = { id: string; name: string; scope: string; passed: boolean; message: string }
type RuleGroup = { group: string; name: string; scope: string; checks?: Check[] }
type Validation = { passed: boolean; groups: RuleGroup[]; 冻结参数: ParamRow[] } & Row
type ContractVersion = {
  id: number
  契约编号: string
  版本号: string
  版本状态: string
  发布日期: string
  冻结参数: ParamRow[]
}
type SandboxResult = {
  sandbox: {
    实例编号: string
    契约编号: string
    契约版本: string
  }
  request: {
    method: string
    url: string
    headers: Record<string, string>
    expected: Record<string, unknown>
    可选错误码: string[]
  }
}
type DocgenResult = {
  docgen: {
    模板编号: string
    契约编号: string
    契约版本: string
  }
  fields: {
    请求参数表: ParamRow[]
    响应字段表: FieldRow[]
    错误码表: ErrorRow[]
  }
}

const ENDPOINT = '/api/apigen'
const columns = ['接口编号', '接口名称', '请求方法', '请求路径', '参数定义', '响应模板', '超时秒数', '接口状态']
const actions = ['发布接口', '废弃接口', '删除接口']
const stats = ref([
  { label: '接口配置总数', value: 0 },
  { label: '已发布接口', value: 0 },
  { label: '已废弃接口', value: 0 },
])

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const workbenchOpen = ref(false)
const loading = ref(false)
const activeRow = ref<Row | null>(null)
const rules = ref<RuleGroup[]>([])
const validation = ref<Validation | null>(null)
const versions = ref<ContractVersion[]>([])
const draftParams = ref('')
const draftTemplate = ref('')
const sandboxResult = ref<SandboxResult | null>(null)
const docgenResult = ref<DocgenResult | null>(null)

const frozenParams = computed<ParamRow[]>(() => validation.value?.['冻结参数'] ?? [])

function formatCell(value: Row[keyof Row]): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'object') return '结构化定义（见契约工作台）'
  return String(value)
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    if (!response.ok) {
      throw new Error('API接口生成动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'API接口生成操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('接口配置列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value[0].value = total.value
    stats.value[1].value = rows.value.filter((row) => row.status === '已发布').length
    stats.value[2].value = rows.value.filter((row) => row.status === '已废弃').length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'API接口生成列表读取失败'
  }
}

async function openWorkbench(row: Row) {
  activeRow.value = row
  workbenchOpen.value = true
  validation.value = null
  versions.value = []
  sandboxResult.value = null
  docgenResult.value = null
  syncDraftFromRow()
  await Promise.all([loadRules(), loadValidation(), loadVersions()])
}

function closeWorkbench() {
  workbenchOpen.value = false
  activeRow.value = null
}

function syncDraftFromRow() {
  if (!activeRow.value) return
  draftParams.value = JSON.stringify(activeRow.value['参数定义'] ?? [], null, 2)
  draftTemplate.value = JSON.stringify(activeRow.value['响应模板'] ?? {}, null, 2)
}

async function loadRules() {
  try {
    const response = await request(`${ENDPOINT}/contract-rules`)
    const payload = await response.json()
    rules.value = payload.rules ?? []
  } catch {
    rules.value = []
  }
}

async function loadValidation() {
  if (!activeRow.value) return
  loading.value = true
  try {
    const response = await request(`${ENDPOINT}/${activeRow.value.id}/contracts/validate`)
    validation.value = await response.json()
  } finally {
    loading.value = false
  }
}

async function publishContract() {
  if (!activeRow.value) return
  loading.value = true
  try {
    const response = await request(`${ENDPOINT}/${activeRow.value.id}/contracts/publish`, { method: 'POST' })
    const payload = await response.json()
    errorMessage.value = payload.ok ? '' : payload.message
    if (payload.ok) {
      await Promise.all([loadValidation(), loadVersions(), reload()])
      activeRow.value = rows.value.find((row) => row.id === (activeRow.value as Row).id) ?? activeRow.value
    } else {
      validation.value = payload.entry?.validation ?? validation.value
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '契约发布失败'
  } finally {
    loading.value = false
  }
}

async function saveDefinitions() {
  if (!activeRow.value) return
  let params: unknown
  let template: unknown
  try {
    params = JSON.parse(draftParams.value)
    template = JSON.parse(draftTemplate.value)
  } catch {
    errorMessage.value = 'JSON 格式不合法，请先修正参数定义或响应模板'
    return
  }
  loading.value = true
  try {
    const response = await request(`${ENDPOINT}/${activeRow.value.id}/definitions`, {
      method: 'PUT',
      body: JSON.stringify({ values: { 参数定义: params, 响应模板: template } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      errorMessage.value = payload.message
      return
    }
    errorMessage.value = ''
    await reload()
    activeRow.value = rows.value.find((row) => row.id === (activeRow.value as Row).id) ?? activeRow.value
    await Promise.all([loadValidation(), loadVersions()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '模板修订保存失败'
  } finally {
    loading.value = false
  }
}

async function loadVersions() {
  if (!activeRow.value) return
  try {
    const response = await request(`${ENDPOINT}/${activeRow.value.id}/contracts`)
    const payload = await response.json()
    versions.value = payload.versions ?? []
  } catch {
    versions.value = []
  }
}

async function runSandbox(version: ContractVersion) {
  if (!activeRow.value) return
  try {
    const response = await request(
      `${ENDPOINT}/${activeRow.value.id}/contracts/${version.id}/sandbox`,
      { method: 'POST', body: JSON.stringify({}) },
    )
    const payload = await response.json()
    if (!payload.ok) {
      errorMessage.value = payload.message
      return
    }
    sandboxResult.value = payload.entry
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '沙箱请求构造失败'
  }
}

async function runDocgen(version: ContractVersion) {
  if (!activeRow.value) return
  try {
    const response = await request(
      `${ENDPOINT}/${activeRow.value.id}/contracts/${version.id}/docgen`,
      { method: 'POST' },
    )
    const payload = await response.json()
    if (!payload.ok) {
      errorMessage.value = payload.message
      return
    }
    docgenResult.value = payload.entry
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '文档生成失败'
  }
}

onMounted(reload)
</script>
