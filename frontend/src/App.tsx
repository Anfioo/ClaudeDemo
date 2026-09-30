import { useEffect, useRef, useState, useCallback } from 'react'

type Role = 'user' | 'assistant'

interface Block {
  index: number
  kind: 'thinking' | 'text' | 'tool_use' | 'other'
  text: string
  done: boolean
}

interface Message {
  id: number
  role: Role
  // assistant messages are assembled from blocks keyed by index
  blocks?: Block[]
  text?: string // for user
  error?: boolean
}

type Status = 'idle' | 'connecting' | 'thinking' | 'typing'

let nextId = 1

export default function App() {
  const [messages, setMessages] = useState<Message[]>([])
  const [status, setStatus] = useState<Status>('idle')
  const [input, setInput] = useState('')
  const [model, setModel] = useState('')
  const [cwd, setCwd] = useState('')
  const wsRef = useRef<WebSocket | null>(null)
  // assistant block index -> message id (so deltas update the right message)
  const blockMap = useRef<Map<number, number>>(new Map())
  const scrollRef = useRef<HTMLDivElement>(null)

  const connect = useCallback(() => {
    setStatus('connecting')
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${location.host}/ws`)
    wsRef.current = ws

    ws.onopen = () => {
      ws.send(JSON.stringify({ type: 'start', cwd, model: model || null }))
    }

    ws.onmessage = (ev) => {
      const data = JSON.parse(ev.data)
      handleEvent(data)
    }

    ws.onclose = () => setStatus('idle')
    ws.onerror = () => setStatus('idle')
  }, [cwd, model])

  const handleEvent = useCallback((data: any) => {
    switch (data.type) {
      case 'ready':
        setStatus('idle')
        break
      case 'system_init':
        setMessages((m) => [
          ...m,
          {
            id: nextId++,
            role: 'assistant',
            text: `Connected · model: ${data.model ?? '?'} · cwd: ${data.cwd ?? '?'}`,
          },
        ])
        break
      case 'user_echo':
        setMessages((m) => [...m, { id: nextId++, role: 'user', text: data.text }])
        setStatus('thinking')
        break
      case 'block_start': {
        // New assistant turn starts; create an assistant message container.
        if (data.kind === 'thinking') setStatus('thinking')
        if (data.kind === 'text') setStatus('typing')
        const msgId = nextId++
        blockMap.current.set(data.index, msgId)
        setMessages((m) => [
          ...m,
          { id: msgId, role: 'assistant', blocks: [{ index: data.index, kind: data.kind, text: '', done: false }] },
        ])
        break
      }
      case 'block_delta': {
        const msgId = blockMap.current.get(data.index)
        if (msgId == null) break
        if (data.kind === 'thinking') setStatus('thinking')
        if (data.kind === 'text') setStatus('typing')
        setMessages((m) =>
          m.map((msg) => {
            if (msg.id !== msgId || !msg.blocks) return msg
            const idx = msg.blocks.findIndex((b) => b.index === data.index)
            const blocks = msg.blocks.slice()
            if (idx >= 0) blocks[idx] = { ...blocks[idx], text: data.text, kind: data.kind }
            else blocks.push({ index: data.index, kind: data.kind, text: data.text, done: false })
            return { ...msg, blocks }
          }),
        )
        break
      }
      case 'block_stop': {
        const msgId = blockMap.current.get(data.index)
        if (msgId == null) break
        setMessages((m) =>
          m.map((msg) =>
            msg.id === msgId && msg.blocks
              ? { ...msg, blocks: msg.blocks.map((b) => (b.index === data.index ? { ...b, done: true } : b)) }
              : msg,
          ),
        )
        break
      }
      case 'result':
        setStatus('idle')
        if (data.is_error) {
          setMessages((m) => [
            ...m,
            { id: nextId++, role: 'assistant', text: `✖ turn error${data.usage ? ' · usage ' + JSON.stringify(data.usage) : ''}`, error: true },
          ])
        }
        break
      case 'error':
        setMessages((m) => [...m, { id: nextId++, role: 'assistant', text: '⚠ ' + data.message, error: true }])
        setStatus('idle')
        break
      case 'log':
        setMessages((m) => [...m, { id: nextId++, role: 'assistant', text: '» ' + data.message, error: false }])
        break
      case 'exit':
        setMessages((m) => [
          ...m,
          { id: nextId++, role: 'assistant', text: `· claude process exited (code ${data.code})`, error: data.code !== 0 },
        ])
        setStatus('idle')
        break
      case 'closed':
        setStatus('idle')
        break
    }
  }, [])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages])

  const send = () => {
    const text = input.trim()
    if (!text) return
    setInput('')
    const existing = wsRef.current
    if (!existing || existing.readyState !== WebSocket.OPEN) {
      connect()
      // wait for open then send
      const waitOpen = setInterval(() => {
        const ws = wsRef.current
        if (ws && ws.readyState === WebSocket.OPEN) {
          clearInterval(waitOpen)
          ws.send(JSON.stringify({ type: 'user_message', text }))
        }
      }, 100)
    } else {
      existing.send(JSON.stringify({ type: 'user_message', text }))
    }
  }

  return (
    <div className="h-full flex flex-col">
      <header className="border-b px-4 py-3 flex items-center gap-3">
        <div className="font-semibold">ClaudeDemo</div>
        <StatusPill status={status} />
        <div className="ml-auto flex items-center gap-2 text-sm text-neutral-500">
          <input
            className="border rounded px-2 py-1 text-xs w-48"
            placeholder="model (sonnet/opus, optional)"
            value={model}
            onChange={(e) => setModel(e.target.value)}
          />
          <input
            className="border rounded px-2 py-1 text-xs w-56"
            placeholder="working dir"
            value={cwd}
            onChange={(e) => setCwd(e.target.value)}
          />
        </div>
      </header>

      <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-6 space-y-4">
        {messages.length === 0 && (
          <div className="text-center text-neutral-400 mt-20">
            Type a message below. Backend spawns local <code>claude</code> CLI in stream-json mode.
          </div>
        )}
        {messages.map((msg) => (
          <MessageView key={msg.id} msg={msg} />
        ))}
      </div>

      <div className="border-t p-4">
        <div className="flex gap-2 max-w-4xl mx-auto">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                send()
              }
            }}
            rows={2}
            placeholder="Message Claude…  (Enter to send, Shift+Enter for newline)"
            className="flex-1 border rounded-lg px-3 py-2 text-sm resize-none focus:outline-none focus:ring-2 focus:ring-neutral-300"
          />
          <button
            onClick={send}
            className="px-4 rounded-lg bg-neutral-900 text-white text-sm hover:bg-neutral-700"
          >
            Send
          </button>
        </div>
      </div>
    </div>
  )
}

function StatusPill({ status }: { status: Status }) {
  const map: Record<Status, { label: string; cls: string }> = {
    idle: { label: 'Idle', cls: 'bg-neutral-100 text-neutral-600' },
    connecting: { label: 'Connecting…', cls: 'bg-amber-100 text-amber-700' },
    thinking: { label: 'Thinking…', cls: 'bg-violet-100 text-violet-700' },
    typing: { label: 'Typing…', cls: 'bg-emerald-100 text-emerald-700' },
  }
  const s = map[status]
  return (
    <span className={`text-xs px-2 py-0.5 rounded-full ${s.cls}`}>
      {s.label}
    </span>
  )
}

function MessageView({ msg }: { msg: Message }) {
  if (msg.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] bg-neutral-900 text-white rounded-2xl rounded-br-sm px-4 py-2 whitespace-pre-wrap text-sm">
          {msg.text}
        </div>
      </div>
    )
  }
  if (msg.error) {
    return <div className="text-sm text-red-600">{msg.text}</div>
  }
  if (!msg.blocks) {
    return <div className="text-sm text-neutral-500">{msg.text}</div>
  }
  return (
    <div className="space-y-2">
      {msg.blocks.map((b) => (
        <BlockView key={b.index} block={b} />
      ))}
    </div>
  )
}

function BlockView({ block }: { block: Block }) {
  if (block.kind === 'thinking') {
    const open = !block.done
    return (
      <details open={open} className="rounded-lg bg-neutral-50 border border-neutral-200 text-sm">
        <summary className="cursor-pointer px-3 py-1.5 text-xs text-neutral-500 select-none">
          {block.done ? `Thought for ${block.text.length} chars` : 'Thinking…'}
        </summary>
        <pre className="px-3 pb-2 pt-0 text-xs text-neutral-600 whitespace-pre-wrap font-mono">
          {block.text || ' '}
        </pre>
      </details>
    )
  }
  if (block.kind === 'tool_use') {
    return (
      <div className="inline-flex items-center gap-1 text-xs bg-blue-50 text-blue-700 border border-blue-200 rounded px-2 py-1">
        🔧 {block.text}
      </div>
    )
  }
  return (
    <div className="text-sm whitespace-pre-wrap leading-relaxed">{block.text || ' '}</div>
  )
}
