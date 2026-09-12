--
-- PostgreSQL database dump
--

\restrict lXhrLjE5xOX4LF7TJwhWZ7vbgSfnzduKXtXPuzmJT1aU2RbIapAxiS73Hes2UEi

-- Dumped from database version 18.6 (Debian 18.6-1.pgdg13+2)
-- Dumped by pg_dump version 18.6 (Debian 18.6-1.pgdg13+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: empty; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.empty (
    label text
);


--
-- Name: measurements; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.measurements (
    id bigint,
    amount numeric(38,18),
    signal double precision,
    label text,
    active boolean,
    optional text
);


--
-- Data for Name: empty; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.empty (label) FROM stdin;
\.


--
-- Data for Name: measurements; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.measurements (id, amount, signal, label, active, optional) FROM stdin;
1	1.230000000000000001	2.5	观测一	t	\N
9007199254740993	-99999999999999999999.123456789012345678	9		f	内容
3	\N	\N	\N	\N	
\.


--
-- PostgreSQL database dump complete
--

\unrestrict lXhrLjE5xOX4LF7TJwhWZ7vbgSfnzduKXtXPuzmJT1aU2RbIapAxiS73Hes2UEi

