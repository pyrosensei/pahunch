export type Order = {id:number;merchant_name:string;delivery_address_summary:string;window_start:string;window_end:string;payment_mode:string;requires_otp:boolean;declared_value_inr:number;status:string};
export type Call = {id:string;order_id:number;state:string;transport:string;customer_confirmed:boolean;outcome?:string;proposal?:any;verdict?:any;courier_card?:any;dispatch_state?:string;started_at?:string;retry_after?:string;remaining_attempts?:number};
export async function api<T>(path:string, options:RequestInit={}):Promise<T>{
  const response=await fetch(path,{...options,headers:{'Content-Type':'application/json',...options.headers}});
  const body=await response.json().catch(()=>null);
  if(!response.ok) throw new Error(typeof body?.detail==='string'?body.detail:`Request failed (${response.status}). Please try again.`);
  return body as T;
}
export const dateTime=(value?:string)=>value?new Date(value).toLocaleString('en-IN',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit',timeZone:'Asia/Kolkata'}):'Not available';
export const label=(value?:string)=>value?value.toLowerCase().replaceAll('_',' ').replace(/^./,x=>x.toUpperCase()):'Not available';
export const terminal=new Set(['COMMITTED','CANCELLED','DECLINED','NO_ANSWER','UNREACHABLE','ESCALATED','TOOL_FAILURE','TURN_BUDGET_EXHAUSTED']);
