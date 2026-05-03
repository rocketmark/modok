export interface Ticket {
  id: string
  subject: string
  content: string
  created_at: string // ISO 8601
}

export interface Note {
  id: string
  ticket_id: string
  author: string
  body: string
  created_at: string // ISO 8601
}
