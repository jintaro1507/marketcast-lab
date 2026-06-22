import { createClient } from 'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.108.2/+esm';

const SUPABASE_URL = 'https://lvsustmfqrxjnfgdtlna.supabase.co';
const SUPABASE_PUBLISHABLE_KEY = 'sb_publishable_IwyvwJjPybtcf1jYiBWPtg_XeMe7fdV';

export const supabase = createClient(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY);
