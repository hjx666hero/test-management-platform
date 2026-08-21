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
      </template>
    </el-drawer>

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
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { listFixSuggestions, reviewFixSuggestion, triggerAutoFix } from '../api'

const rows = ref([])
const loading = ref(false)
const statusFilter = ref('')
const drawer = ref(false)
const selected = ref(null)

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
    await triggerAutoFix({ ...newFixForm.value })
    ElMessage.success('已提交后台执行,结果将实时出现在下方列表')
    newFixDialog.value = false
  } finally {
    fixing.value = false
  }
}

onMounted(() => {
  load()
  timer = setInterval(load, 5000) // 5s 轮询:Agent 在后台生成补丁后自动刷新
})
onBeforeUnmount(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
.page-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.page-head h2 { margin: 0; }
.head-actions { display: flex; align-items: center; }
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
