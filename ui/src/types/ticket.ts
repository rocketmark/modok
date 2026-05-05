export interface Ticket {
  id: string
  subject: string
  content: string
  created_at: string // ISO 8601
  user_created?: true  // present only on tickets created via the New Ticket button
}

export interface Note {
  id: string
  ticket_id: string
  author: string
  body: string
  created_at: string // ISO 8601
}
