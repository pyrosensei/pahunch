import type { VoiceSnapshot } from './controller';

/** Present controller state without creating a second audio or connection state machine. */
export function conversationState(state: VoiceSnapshot) {
  if (state.session?.courier_card || state.session?.state === 'COMMITTED') return { key: 'complete', label: 'Plan committed' };
  if (state.connecting) return { key: 'connecting', label: 'Connecting' };
  if (state.error) return { key: 'failed', label: state.connected ? 'Needs attention' : 'Connection unavailable' };
  if (state.processing) return { key: 'processing', label: 'Preparing your reply' };
  if (state.plannerError) return { key: 'failed', label: 'Reply needs attention' };
  if (state.speaking) return { key: 'speaking', label: 'Pahunch is speaking' };
  if (state.hint === 'Go ahead, I’m listening.') return { key: 'interrupted', label: 'Interrupted · your turn' };
  if (state.listening) return { key: 'listening', label: 'Listening to you' };
  return { key: 'idle', label: state.connected ? 'Ready for your reply' : 'Ready when you are' };
}
