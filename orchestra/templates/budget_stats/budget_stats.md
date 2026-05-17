own ctx     {tokens_in} / {max_context}    {own_bar} {pct}%
shared pool {paid} / {max_tokens}    {shared_bar} {shared_pct}%  (steps {steps_used}/{max_steps})
wall clock  {elapsed} / {wall_max}    {wall_bar} {wall_pct}%

spawn: ~{spawn_carved} tokens + ~{spawn_carved_steps} steps → ~{spawn_return} back{subagents}
