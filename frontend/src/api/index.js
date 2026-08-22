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
// 提交失败用例给 Agent 后台修复(ReAct 循环)
export const triggerAutoFix = (payload) => http.post('/fixes/auto-fix', payload)
// 修复建议列表(可传 { status, case_name } 过滤)
export const listFixSuggestions = (params) => http.get('/fixes/suggestions', { params })
// 人工审核:status = 'applied'(应用补丁到源文件) / 'rejected'(拒绝)
export const reviewFixSuggestion = (id, status) => http.patch(`/fixes/suggestions/${id}`, { status })

// ===== Agent 看板与对话 =====
// 成本看板:今日运行次数/总花费/平均耗时
export const getAgentStats = () => http.get('/agent/stats')

/**
 * 问 Agent(流式):针对补丁提问,基于 ReAct 轨迹流式返回解释。
 * 用原生 fetch 而非 axios:axios 拦截器会等整个响应,无法逐段读取流。
 * @param {number} patchId 补丁 ID
 * @param {string} question 用户问题
 * @param {(text: string) => void} onChunk 每收到一段文本的回调(打字机效果)
 */
export async function askAgentStream(patchId, question, onChunk) {
  const resp = await fetch('/api/agent/ask', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ patch_id: patchId, question }),
  })
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`
    try {
      detail = (await resp.json()).detail || detail
    } catch { /* 忽略解析失败 */ }
    throw new Error(detail)
  }
  // 流式读取:浏览器 ReadableStream 逐段解码,实时回调
  const reader = resp.body.getReader()
  const decoder = new TextDecoder('utf-8')
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    const text = decoder.decode(value, { stream: true })
    if (text) onChunk(text)
  }
}

/**
 * 订阅 Agent 实时思考流(SSE):逐轮推送 ReAct 轨迹(思考/工具结果)。
 * 用原生 EventSource(SSE 标准,仅支持 GET)。
 * @param {string} traceId 提交修复任务时返回的 trace_id
 * @param {(evt: object) => void} onEvent 每条轨迹事件回调 {id, round, thought, tool_result}
 * @param {() => void} onDone 流结束回调([DONE] 或连接关闭)
 * @returns {EventSource} 可调 .close() 手动断开
 */
export function streamAgentTrace(traceId, onEvent, onDone) {
  const es = new EventSource(`/api/agent/trace/${traceId}/events`)
  es.onmessage = (msg) => {
    if (msg.data === '[DONE]') { es.close(); onDone && onDone(); return }
    try { onEvent(JSON.parse(msg.data)) } catch { /* 忽略解析失败 */ }
  }
  es.onerror = () => { es.close(); onDone && onDone() }  // 断线/服务重启:静默收尾
  return es
}

export default http
