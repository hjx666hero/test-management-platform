import axios from 'axios'
import { ElMessage } from 'element-plus'

const http = axios.create({ baseURL: '/api', timeout: 30000 })

http.interceptors.response.use(
  (resp) => resp.data,
  (err) => {
    ElMessage.error(err?.response?.data?.detail || err.message || '请求失败')
    return Promise.reject(err)
  }
)

// ===== 任务 =====
export const listTasks = () => http.get('/tasks')
export const getTask = (id) => http.get(`/tasks/${id}`)
export const createTask = (payload) => http.post('/tasks', payload)
export const getReport = (id) => http.get(`/tasks/${id}/report`)

// ===== Auto-Fix Agent =====
// 提交失败用例给 Agent 后台修复(ReAct 循环,最多 3 轮)
export const triggerAutoFix = (payload) => http.post('/fixes/auto-fix', payload)
// 修复建议列表(可传 { status, case_name } 过滤)
export const listFixSuggestions = (params) => http.get('/fixes/suggestions', { params })
// 人工审核:status = 'applied'(应用补丁到源文件) / 'rejected'(拒绝)
export const reviewFixSuggestion = (id, status) => http.patch(`/fixes/suggestions/${id}`, { status })

export default http
