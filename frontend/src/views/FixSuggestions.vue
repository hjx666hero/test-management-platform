<template>
  <div>
    <div class="page-head">
      <h2>修复建议</h2>
      <div class="head-actions">
        <el-radio-group v-model="statusFilter" size="small" @change="load">
          <el-radio-button value="">全部</el-radio-button>
          <el-radio-button value="pending_review">待审核</el-radio-button>
          <el-radio-button value="applied">已应用</el-radio-button>
          <el-radio-button value="rejected">已拒绝</el-radio-button>
        </el-radio-group>
        <el-button size="small" style="margin-left: 8px" @click="load">刷新</el-button>
        <el-button size="small" type="primary" @click="openNewFix">新建修复任务</el-button>
      </div>
    </div>

    <!-- Agent 成本看板:今日运行次数 / 总花费 / 平均耗时 -->
    <el-row :gutter="12" class="stats-row" v-if="stats">
      <el-col :span="8">
        <el-card shadow="never" class="stat-card">
          <div class="stat-label">今日运行次数</div>
          <div class="stat-value">{{ stats.runs }}</div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never" class="stat-card">
          <div class="stat-label">今日总花费 (¥)</div>
          <div class="stat-value">{{ stats.total_cost_rmb }}</div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never" class="stat-card">
          <div class="stat-label">平均耗时 (秒)</div>
          <div class="stat-value">{{ stats.avg_duration_s }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never">
      <el-table :data="rows" v-loading="loading" stripe>
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column prop="case_name" label="用例" min-width="170" show-overflow-tooltip />
        <el-table-column prop="file_path" label="文件" min-width="190" show-overflow-tooltip />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="Agent 验证" width="110">
          <template #default="{ row }">
            <el-tag v-if="row.verified === true" type="success" size="small">验证通过</el-tag>
            <el-tag v-else-if="row.verified === false" type="danger" size="small">验证未过</el-tag>
            <span v-else class="muted">未验证</span>
          </template>
        </el-table-column>
        <el-table-column label="AI 评审" width="120">
          <template #default="{ row }">
            <el-tag v-if="row.judge_verdict === 'approve'" type="success" size="small">{{ row.judge_score }} 分·通过</el-tag>
            <el-tag v-else-if="row.judge_verdict === 'warn'" type="warning" size="small">{{ row.judge_score }} 分·注意</el-tag>
            <el-tag v-else-if="row.judge_verdict === 'reject'" type="danger" size="small">{{ row.judge_score }} 分·否决</el-tag>
            <span v-else class="muted">未评审</span>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="生成时间" width="170" />
        <el-table-column label="操作" width="190" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="primary" link @click="openDetail(row)">详情</el-button>
            <template v-if="row.status === 'pending_review'">
              <el-button size="small" type="success" link @click="review(row, 'applied')">通过并应用</el-button>
              <el-button size="small" type="danger" link @click="review(row, 'rejected')">拒绝</el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>
      <div class="muted tip">Agent 生成的补丁均为待审核状态,不会自动修改源码;「通过并应用」才会写入文件</div>
    </el-card>

    <!-- 建议详情抽屉 -->
    <el-drawer v-model="drawer" :title="selected ? `修复建议 #${selected.id}` : ''" size="56%">
      <template v-if="selected">
        <h4 style="margin: 0 0 8px">修复理由(AI)</h4>
        <div class="ai-box">{{ selected.explanation }}</div>

        <!-- LLM-as-judge 自动评审结果 -->
        <template v-if="selected.judge_verdict">
          <h4 style="margin: 16px 0 8px">AI 评审(LLM-as-judge)</h4>
          <div class="judge-box" :class="selected.judge_verdict">
            <el-tag :type="{ approve: 'success', warn: 'warning', reject: 'danger' }[selected.judge_verdict]" size="small">
              {{ selected.judge_score }} 分 · {{ { approve: '通过', warn: '注意', reject: '否决' }[selected.judge_verdict] }}
            </el-tag>
            <div class="judge-comment">{{ selected.judge_comment }}</div>
          </div>
        </template>
        <div v-else class="muted" style="margin-top: 12px; font-size: 12px">AI 评审进行中或未启用(生成补丁后数秒内出结果)</div>

        <h4 style="margin: 16px 0 8px">补丁内容(unified diff)</h4>
        <pre class="code-block diff">{{ selected.diff }}</pre>

        <h4 style="margin: 16px 0 8px">验证输出</h4>
        <pre class="code-block">{{ selected.verify_output || '未验证(Agent 未执行带补丁的运行)' }}</pre>

        <h4 style="margin: 16px 0 8px">原始代码</h4>
        <pre class="code-block">{{ selected.original_code }}</pre>

        <h4 style="margin: 16px 0 8px">修复后代码</h4>
        <pre class="code-block">{{ selected.fixed_code }}</pre>

        <div v-if="selected.status === 'pending_review'" style="margin-top: 20px">
          <el-button type="success" @click="review(selected, 'applied')">通过并应用到源文件</el-button>
          <el-button type="danger" @click="review(selected, 'rejected')">拒绝</el-button>
        </div>
        <div style="margin-top: 12px">
          <el-button type="primary" plain @click="openAsk(selected)">问 Agent:为什么这样改?</el-button>
        </div>
      </template>
    </el-drawer>

    <!-- 问 Agent 对话框:基于该补丁的 ReAct 轨迹流式解释 -->
    <el-dialog v-model="askDialog" :title="`问 Agent — 补丁 #${askPatchId || ''}`" width="640px">
      <div class="ask-history" ref="askHistoryEl">
        <div v-for="(m, i) in askMessages" :key="i" :class="['ask-msg', m.role]">
          <div class="ask-bubble">{{ m.text }}</div>
        </div>
        <div v-if="asking" class="ask-msg agent">
          <div class="ask-bubble">{{ askStreamingText }}<span class="cursor">▍</span></div>
        </div>
      </div>
      <div style="display: flex; gap: 8px; margin-top: 12px">
        <el-input
          v-model="askInput"
          placeholder="例如:这个修复的根因是什么?会影响其他用例吗?"
          :disabled="asking"
          @keyup.enter="sendAsk"
        />
        <el-button type="primary" :loading="asking" @click="sendAsk">发送</el-button>
      </div>
    </el-dialog>

    <!-- Agent 实时思考流(SSE):提交修复任务后逐轮展示 ReAct 轨迹 -->
    <el-dialog v-model="traceDialog" title="Agent 修复进行中(实时思考流)" width="680px" :close-on-click-modal="false">
      <div class="trace-stream" ref="traceStreamEl">
        <div v-for="t in traceEvents" :key="t.id" class="trace-item">
          <div class="trace-head">
            <el-tag size="small" :type="t.round === 0 ? 'info' : 'primary'">
              {{ t.round === 0 ? '启动' : `第 ${t.round} 轮` }}
            </el-tag>
            <span class="trace-time">{{ t.time }}</span>
          </div>
          <div class="trace-thought">{{ t.thought }}</div>
          <pre v-if="t.tool_result" class="trace-tools">{{ t.tool_result }}</pre>
        </div>
        <div v-if="traceRunning" class="trace-waiting">Agent 思考中…<span class="cursor">▍</span></div>
        <div v-if="traceDone && !traceRunning" class="trace-done">✓ 本次运行结束,补丁与评审结果已进入下方列表</div>
      </div>
      <template #footer>
        <el-button v-if="traceRunning" @click="stopTrace">后台继续,关闭窗口</el-button>
        <el-button v-else type="primary" @click="traceDialog = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 新建修复任务对话框(独立触发,不依赖任务报告) -->
    <el-dialog v-model="newFixDialog" title="新建 AI 修复任务" width="580px">
      <el-form label-width="90px">
        <el-form-item label="用例名" required>
          <el-input v-model="newFixForm.case_name" placeholder="pytest 用例函数名,如 test_login_success" />
        </el-form-item>
        <el-form-item label="文件路径" required>
          <el-input v-model="newFixForm.file_path" placeholder="相对项目一根目录,如 testcases/test_login.py" />
        </el-form-item>
        <el-form-item label="错误日志">
          <el-input v-model="newFixForm.error_log" type="textarea" :rows="4" placeholder="可留空,Agent 会先运行用例复现失败" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="newFixDialog = false">取消</el-button>
        <el-button type="primary" :loading="fixing" @click="submitNewFix">提交修复</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { askAgentStream, getAgentStats, listFixSuggestions, reviewFixSuggestion, streamAgentTrace, triggerAutoFix } from '../api'

const rows = ref([])
const loading = ref(false)
const statusFilter = ref('')
const drawer = ref(false)
const selected = ref(null)

// ===== 成本看板(/stats) =====
const stats = ref(null)

async function loadStats() {
  try {
    stats.value = await getAgentStats()  // 看板失败静默(后端自身已降级为零值)
  } catch { /* 忽略:看板不阻塞列表 */ }
}

// ===== 列表加载与轮询(Agent 后台执行时,轮询可看到补丁实时出现) =====
let timer = null

async function load() {
  loading.value = true
  try {
    rows.value = await listFixSuggestions(statusFilter.value ? { status: statusFilter.value } : {})
  } finally {
    loading.value = false
  }
}

const statusType = (s) => ({ pending_review: 'warning', applied: 'success', rejected: 'info' }[s] || 'info')
const statusText = (s) => ({ pending_review: '待审核', applied: '已应用', rejected: '已拒绝' }[s] || s)

// ===== 详情 =====
function openDetail(row) {
  selected.value = row
  drawer.value = true
}

// ===== 人工审核 =====
async function review(row, status) {
  const isApply = status === 'applied'
  try {
    await ElMessageBox.confirm(
      isApply ? '应用后补丁将写入源文件(与 Agent 验证时的替换一致),确认应用?' : '确认拒绝该修复建议?',
      isApply ? '应用确认' : '拒绝确认',
      { type: 'warning' }
    )
  } catch {
    return // 用户取消
  }
  await reviewFixSuggestion(row.id, status)
  ElMessage.success(isApply ? '补丁已应用到源文件' : '已拒绝')
  drawer.value = false
  load()
}

// ===== 新建修复任务(独立触发入口) =====
const newFixDialog = ref(false)
const fixing = ref(false)
const newFixForm = ref({ file_path: '', case_name: '', error_log: '' })

function openNewFix() {
  newFixForm.value = { file_path: '', case_name: '', error_log: '' }
  newFixDialog.value = true
}

async function submitNewFix() {
  const { file_path, case_name } = newFixForm.value
  if (!file_path.trim() || !case_name.trim()) {
    ElMessage.warning('请填写用例名与文件路径')
    return
  }
  fixing.value = true
  try {
    const resp = await triggerAutoFix({ ...newFixForm.value })
    ElMessage.success('已提交后台执行')
    newFixDialog.value = false
    // 后端返回 trace_id → 立即打开实时思考流窗口(SSE 逐轮观看 Agent 决策)
    if (resp.trace_id) startTrace(resp.trace_id)
  } finally {
    fixing.value = false
  }
}

// ===== Agent 实时思考流(SSE) =====
const traceDialog = ref(false)
const traceEvents = ref([])      // [{id, round, thought, tool_result, time}]
const traceRunning = ref(false)
const traceDone = ref(false)
const traceStreamEl = ref(null)
let traceEs = null               // 当前 EventSource

function startTrace(traceId) {
  stopTrace()                    // 断开上一次的订阅(如有)
  traceEvents.value = []
  traceDone.value = false
  traceRunning.value = true
  traceDialog.value = true
  traceEs = streamAgentTrace(
    traceId,
    (evt) => {
      traceEvents.value.push({ ...evt, time: new Date().toLocaleTimeString() })
      scrollTrace()
    },
    () => {
      traceRunning.value = false
      traceDone.value = true
      load()                     // 流结束:补丁已入库,刷新列表与看板
      loadStats()
    }
  )
}

function stopTrace() {
  if (traceEs) { traceEs.close(); traceEs = null }
  traceRunning.value = false
}

onMounted(() => {
  load()
  loadStats()
  timer = setInterval(() => { load(); loadStats() }, 5000) // 5s 轮询:补丁与看板实时刷新
})

// ===== 问 Agent(/ask 流式对话) =====
const askDialog = ref(false)
const askPatchId = ref(null)
const askInput = ref('')
const asking = ref(false)
const askMessages = ref([])        // [{role: 'user'|'agent', text}]
const askStreamingText = ref('')
const askHistoryEl = ref(null)

function openAsk(row) {
  askPatchId.value = row.id
  askMessages.value = []            // 每次打开重置对话
  askStreamingText.value = ''
  askInput.value = ''
  askDialog.value = true
}

async function sendAsk() {
  const question = askInput.value.trim()
  if (!question || asking.value) return
  askInput.value = ''
  askMessages.value.push({ role: 'user', text: question })
  asking.value = true
  askStreamingText.value = ''
  try {
    // 流式接收:每一段文本实时追加(打字机效果)
    await askAgentStream(askPatchId.value, question, (chunk) => {
      askStreamingText.value += chunk
      scrollToBottom()
    })
    askMessages.value.push({ role: 'agent', text: askStreamingText.value })
  } catch (e) {
    askMessages.value.push({ role: 'agent', text: `生成失败: ${e.message}` })
  } finally {
    askStreamingText.value = ''
    asking.value = false
    scrollToBottom()
  }
}

function scrollToBottom() {
  nextTick(() => {
    if (askHistoryEl.value) askHistoryEl.value.scrollTop = askHistoryEl.value.scrollHeight
  })
}

function scrollTrace() {
  nextTick(() => {
    if (traceStreamEl.value) traceStreamEl.value.scrollTop = traceStreamEl.value.scrollHeight
  })
}

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  stopTrace()  // 组件卸载断开 SSE,防泄漏
})
</script>

<style scoped>
.page-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.page-head h2 { margin: 0; }
.head-actions { display: flex; align-items: center; }
.stats-row { margin-bottom: 16px; }
.stat-card { text-align: center; }
.stat-label { font-size: 13px; color: #909399; }
.stat-value { font-size: 24px; font-weight: 600; color: #409eff; margin-top: 6px; }
/* 问 Agent 对话 */
.ask-history { height: 320px; overflow-y: auto; background: #f6f8fa; border-radius: 6px; padding: 12px; }
.ask-msg { display: flex; margin-bottom: 10px; }
.ask-msg.user { justify-content: flex-end; }
.ask-bubble { max-width: 78%; padding: 8px 12px; border-radius: 8px; font-size: 13px; line-height: 1.7; white-space: pre-wrap; word-break: break-word; }
.ask-msg.user .ask-bubble { background: #409eff; color: #fff; }
.ask-msg.agent .ask-bubble { background: #fff; border: 1px solid #e4e7ed; }
.cursor { animation: blink 0.8s infinite; }
@keyframes blink { 50% { opacity: 0; } }
/* SSE 实时思考流 */
.trace-stream { height: 380px; overflow-y: auto; background: #1e1e2e; border-radius: 6px; padding: 14px; }
.trace-item { margin-bottom: 14px; }
.trace-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.trace-time { font-size: 11px; color: #6c7086; }
.trace-thought { color: #cdd6f4; font-size: 13px; line-height: 1.7; white-space: pre-wrap; word-break: break-word; }
.trace-tools { margin: 6px 0 0; padding: 8px; background: #181825; border-radius: 4px; color: #a6adc8; font-size: 11px; max-height: 140px; overflow: auto; white-space: pre-wrap; word-break: break-all; }
.trace-waiting { color: #89b4fa; font-size: 13px; }
.trace-done { color: #a6e3a1; font-size: 13px; margin-top: 8px; }
/* LLM-as-judge 评审框 */
.judge-box { border-radius: 6px; padding: 12px; border: 1px solid #dcdfe6; }
.judge-box.approve { background: #f0f9eb; border-color: #b3e19d; }
.judge-box.warn { background: #fdf6ec; border-color: #f3d19e; }
.judge-box.reject { background: #fef0f0; border-color: #fab6b6; }
.judge-comment { margin-top: 8px; font-size: 13px; line-height: 1.7; white-space: pre-wrap; color: #303133; }
.muted { color: #c0c4cc; }
.tip { font-size: 12px; margin-top: 8px; }
.code-block {
  background: #f6f8fa; border-radius: 6px; padding: 12px;
  font-size: 12px; overflow: auto; max-height: 240px;
  white-space: pre-wrap; word-break: break-all;
}
.diff { background: #fffbe6; border: 1px solid #f3d19e; }
.ai-box {
  background: #f0f9eb; border: 1px solid #b3e19d; border-radius: 6px;
  padding: 12px; font-size: 13px; line-height: 1.7; white-space: pre-wrap;
}
</style>
