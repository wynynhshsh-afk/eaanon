-- فرمت پیام‌ها (بولد، ایتالیک، لینک‌دار، mono و ...) به‌صورت JSON کنار متن ذخیره می‌شه.
-- یک‌بار در Supabase > SQL Editor اجرا کن. اجرای دوباره‌اش بی‌خطره.
-- (تا قبل از اجرا ربات بدون خطا کار می‌کنه ولی پیام‌ها رو بدون فرمت تحویل می‌ده.)
alter table public.anonymous_messages
    add column if not exists entities text;

notify pgrst, 'reload schema';
