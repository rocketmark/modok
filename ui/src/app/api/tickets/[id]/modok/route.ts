import { NextResponse } from 'next/server'
import { loadConfig, ConfigError } from '@/lib/config'
import { deleteRun } from '@/lib/data'
import { runModokStream } from '@/lib/modok-bridge'

// @spec DEMO-BRIDGE-001 through DEMO-BRIDGE-015
export async function POST(_request: Request, { params }: { params: { id: string } }) {
  try {
    loadConfig()
  } catch (err) {
    if (err instanceof ConfigError) {
      return NextResponse.json({ message: err.message }, { status: 503 })
    }
    throw err
  }

  const encoder = new TextEncoder()
  const stream = new ReadableStream({
    async start(controller) {
      const send = (data: object) => {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(data)}\n\n`))
      }
      try {
        await runModokStream(params.id, send)
      } catch (err) {
        send({ type: 'complete', run: { ticket_id: params.id, status: 'failed', error: 'timeout_or_crash' } })
      } finally {
        controller.close()
      }
    },
  })

  return new Response(stream, {
    headers: {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      'Connection': 'keep-alive',
    },
  })
}

export async function DELETE(_request: Request, { params }: { params: { id: string } }) {
  try {
    loadConfig()
  } catch (err) {
    if (err instanceof ConfigError) {
      return NextResponse.json({ message: err.message }, { status: 503 })
    }
    throw err
  }
  deleteRun(params.id)
  return new NextResponse(null, { status: 204 })
}
