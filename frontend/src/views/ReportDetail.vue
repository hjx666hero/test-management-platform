<template>
  <div>
    <el-page-header content="报告详情" @back="$router.push('/tasks')" style="margin-bottom: 16px" />

    <!-- 任务汇总 -->
    <el-card shadow="never" class="summary" v-if="report">
      <el-descriptions :column="4" border size="small">
        <el-descriptions-item label="任务">{{ report.task.name }}</el-descriptions-item>
        <el-descriptions-item label="状态">
          <el-tag :type="statusType(report.task.status)" size="small">{{ statusText(report.task.status) }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="环境">{{ report.task.env_url }}</el-descriptions-item>
        <el-descriptions-item label="耗时">{{ report.task.duration_s }}s</el-descriptions-item>
        <el-descriptions-item label="通过率">
          <b :style="{ color: passColor }">{{ passRate }}%</b>
          ({{ report.task.passed }}/{{ report.task.total }})
        </el-descriptions-item>
        <el-descriptions-item label="失败数">
          <b style="color: #f56c6c">{{ report.task.failed }}</b>
        </el-descriptions-item>
        <el-descriptions-item label="标签">
          <el-tag v-for="t in report.task.tags" :key="t" size="small" class="tag">{{ t }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="错误" v-if="report.task.error">
          <el-tooltip :content="report.task.error"><span class="err-text">{{ report.task.error }}</span></el-tooltip>
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <!-- 用例结果 -->
    <el-card shadow="never" class="result-card">
      <template #header>用例结果 ({{ report?.results.length || 0 }})</template>
      <el-table :data="report?.results || []" v-loading="loading" stripe>
        <el-table-column prop="case_name" label="用例" min-width="220" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.status === 'passed' ? 'success' : 'danger'" size="small">
              {{ row.status === 'passed' ? '通过' : '失败' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="duration_ms" label="耗时" width="90">
          <template #default="{ row }">{{ row.duration_ms }}ms</template>
        </el-table-column>
        <el-table-column label="AI 分析" width="100">
          <template #default="{ row }">
            <el-tag v-if="row.ai_status === 'done'" type="success" size="small">已完成</el-tag>
            <el-tag v-else-if="row.ai_status === 'pending'" type="warning" size="small">生成中</el-tag>
            <el-tag v-else-if="row.ai_status === 'failed'" type="danger" size="small">失败</el-tag>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="primary" link @click="openDetail(row)">详情</el-button>
            <el-button v-if="row.status === 'failed'" size="small" type="warning" link @click="openFixDialog(row)">
              AI 修复
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- AI 自动修复提交对话框 -->
    <el-dialog v-model="fixDialog" title="AI 自动修复" width="580px">
      <el-form label-width="90px">
        <el-form-item label="用例名">
          <el-input v-model="fixForm.case_name" placeholder="pytest 用例函数名,如 test_login_success" />
        </el-form-item>
        <el-form-item label="文件路径" required>
          <el-input v-model="fixForm.file_path" placeholder="相对项目一根目录,如 testcases/test_login.py" />
          <div class="hint">Agent 将读取该文件、生成最小修复补丁并验证;源码仅在人工审核通过后才修改</div>
        </el-form-item>
        <el-form-item label="错误日志">
          <el-input v-model="fixForm.error_log" type="textarea" :rows="4" placeholder="可留空,Agent 会先运行用例复现失败" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="fixDialog = false">取消</el-button>
        <el-button type="primary" :loading="fixing" @click="submitFix">提交修复</el-button>
      </template>
    </el-dialog>

    <!-- 失败详情抽屉 -->
    <el-drawer v-model="drawer" :title="selected?.case_name" size="46%">
      <template v-if="selected">
        <h4 style="margin: 0 0 8px">错误信息</h4>
        <pre class="code-block">{{ selected.error_message || '无' }}</pre>

        <h4 style="margin: 16px 0 8px">请求快照</h4>
        <pre class="code-block">{{ JSON.stringify(selected.request_snapshot, null, 2) || '无' }}</pre>

        <h4 style="margin: 16px 0 8px">响应快照</h4>
        <pre class="code-block">{{ JSON.stringify(selected.response_snapshot, null, 2) || '无' }}</pre>

        <h4 style="margin: 16px 0 8px">AI 根因分析</h4>
        <el-alert v-if="!selected.ai_analysis" :title="aiPlaceholder(selected.ai_status)" type="info" :closable="false" />
        <div v-else class="ai-box">{{ selected.ai_analysis }}</div>
      </template>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getReport, triggerAutoFix } from '../api'

const props = defineProps({ id: { type: [String, Number], required: true } })

const report = ref(null)
const loading = ref(false)
const drawer = ref(false)
const selected = ref(null)
let timer = null

const statusType = (s) => ({ running: 'warning', success: 'success', failed: 'danger', pending: 'info' }[s] || 'info')
const statusText = (s) => ({ running: '执行中', success: '通过', failed: '失败', pending: '排队中' }[s] || s)

const passRate = computed(() => {
  if (!report.value || !report.value.task.total) return 0
  return (report.value.task.passed / report.value.task.total * 100).toFixed(1)
})
const passColor = computed(() => {
  const p = parseFloat(passRate.value)
  return p >= 90 ? '#67c23a' : p >= 70 ? '#e6a23c' : '#f56c6c'
})

const aiPlaceholder = (s) =>
  ({ pending: 'AI 分析生成中...', failed: 'AI 分析调用失败', none: '未执行(通过用例不分析)' }[s] || s)

function openDetail(row) {
  selected.value = row
  drawer.value = true
}

// ===== AI 自动修复 =====
const fixDialog = ref(false)
const fixing = ref(false)
const fixForm = ref({ file_path: '', case_name: '', error_log: '' })
let fixRow = null  // 触发来源行(携带 result_id 供补丁追溯)

function openFixDialog(row) {
  fixRow = row
  // 预填用例名与错误日志;文件路径由用户指定(Agent 的工具只能访问项目一目录)
  fixForm.value = { file_path: '', case_name: row.case_name, error_log: row.error_message || '' }
  fixDialog.value = true
}

async function submitFix() {
  if (!fixForm.value.file_path.trim()) {
    ElMessage.warning('请填写失败用例所在文件路径')
    return
  }
  fixing.value = true
  try {
    await triggerAutoFix({ ...fixForm.value, task_id: Number(props.id), result_id: fixRow?.id })
    ElMessage.success('已提交后台执行,请前往「修复建议」页查看进度与结果')
    fixDialog.value = false
  } finally {
    fixing.value = false
  }
}

async function load() {
  loading.value = true
  try {
    report.value = await getReport(props.id)
    // 执行中则轮询刷新;已完成停止轮询
    if (report.value.task.status === 'running' || report.value.task.status === 'pending') {
      startPolling()
    } else {
      stopPolling()
    }
  } finally {
    loading.value = false
  }
}

function startPolling() {
  stopPolling()
  timer = setInterval(load, 3000)
}
function stopPolling() {
  if (timer) { clearInterval(timer); timer = null }
}

onMounted(load)
onBeforeUnmount(stopPolling)
</script>

<style scoped>
.summary { margin-bottom: 16px; }
.result-card { margin-bottom: 16px; }
.tag { margin-right: 4px; }
.muted { color: #c0c4cc; }
.hint { font-size: 12px; color: #909399; line-height: 1.5; margin-top: 4px; }
.err-text { color: #f56c6c; font-size: 12px; }
.code-block {
  background: #f6f8fa; border-radius: 6px; padding: 12px;
  font-size: 12px; overflow: auto; max-height: 220px;
  white-space: pre-wrap; word-break: break-all;
}
.ai-box {
  background: #f0f9eb; border: 1px solid #b3e19d; border-radius: 6px;
  padding: 12px; font-size: 13px; line-height: 1.7; white-space: pre-wrap;
}
</style>
