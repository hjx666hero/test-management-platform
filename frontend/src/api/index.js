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

export default http
