-- Роль agent_ro для pg_read: чтение таблиц приложения без прав записи.
--
-- Выполнить суперпользователем в КАЖДОЙ базе, которую агент должен читать
-- (роль общая на кластер, права на таблицы — у каждой базы свои):
--   docker exec -i <контейнер> psql -U postgres -d <база> -v ON_ERROR_STOP=1 < skills/db/agent_ro.sql
-- Повторный запуск безопасен.
--
-- Не pg_read_all_data: она открывает и pg_authid с хешами паролей.
-- Пароля у роли нет: pg_read ходит через локальный сокет внутри контейнера
-- (в официальном образе postgres это `local all all trust`).

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_ro') THEN
    CREATE ROLE agent_ro LOGIN;
  END IF;
END$$;

ALTER ROLE agent_ro NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE agent_ro SET default_transaction_read_only = on;

-- SELECT на существующие таблицы всех схем приложения и на будущие таблицы
-- их нынешних владельцев (миграции создают новые).
DO $$
DECLARE
  s text;
  o text;
BEGIN
  FOR s IN
    SELECT nspname FROM pg_namespace
    WHERE nspname NOT LIKE 'pg\_%' AND nspname <> 'information_schema'
  LOOP
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO agent_ro', s);
    EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO agent_ro', s);
    FOR o IN
      SELECT DISTINCT pg_get_userbyid(relowner) FROM pg_class WHERE relnamespace = s::regnamespace
    LOOP
      EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT SELECT ON TABLES TO agent_ro', o, s);
    END LOOP;
  END LOOP;
END$$;
