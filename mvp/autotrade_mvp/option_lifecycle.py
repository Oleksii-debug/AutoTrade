            if len(same_identity) != 1:
                raise OptionLifecycleConflict(
                    "external lifecycle identity appears more than once"
                )
            saved = self._payload(same_identity[0])
            if (
                saved.get("observation_digest") != observation_digest
                or saved.get("instrument_digest") != instrument_digest
                or saved.get("provider_evidence_digest")
                != provider_evidence_digest
            ):
                raise OptionLifecycleConflict(
                    "external lifecycle identity was reused with changed evidence"
                )
            return OptionLifecycleApplyResult(
                lifecycle_event_id=str(same_identity[0]["event_id"]),
                inserted=False,
                active_transaction_ids=tuple(saved.get("active_transaction_ids", ())),
                reversal_transaction_ids=tuple(
                    saved.get("reversal_transaction_ids", ())
                ),
                corrected_external_event_id=saved.get(
                    "corrects_external_event_id"
                ),
            )

        economic_cut = DurableProviderEconomicBook.read_cut(self.economic_book)

        prior_event: Mapping[str, Any] | None = None
        prior_payload: Mapping[str, Any] | None = None
        root_external_id = observation.external_event_id
        old_active_transactions: tuple[JournalTransaction, ...] = ()

        if observation.corrects_external_event_id is not None:
            matches = [
                event
                for event in events
                if (
                    self._payload(event).get("external_event_id")
                    == observation.corrects_external_event_id
                    and self._payload(event).get("provider_environment")
                    == observation.provider_environment
                )
            ]
            if len(matches) != 1:
                raise OptionLifecycleConflict(
                    "correction target must identify exactly one prior lifecycle event"
                )
            if any(
                self._payload(event).get("corrects_external_event_id")
                == observation.corrects_external_event_id
                for event in events
            ):
                raise OptionLifecycleConflict(
                    "correction target already has a replacement; correct the latest event instead"
                )
            prior_event = matches[0]
            prior_payload = self._payload(prior_event)
            if prior_payload.get("instrument_version") != observation.instrument_version:
                raise OptionLifecycleConflict(
                    "correction cannot change instrument version identity"
                )
            if prior_payload.get("instrument_digest") != instrument_digest:
                raise OptionLifecycleConflict(
                    "correction cannot change instrument version authority"
                )
            if prior_payload.get("event_kind") != observation.event_kind:
                raise OptionLifecycleConflict("correction cannot change lifecycle event kind")
            if prior_payload.get("effective_at") != _utc_text(observation.effective_at):
                raise OptionLifecycleConflict(
                    "correction must preserve original economic effective time"
                )
            if prior_payload.get("observed_at", "") > _utc_text(observation.observed_at):
                raise OptionLifecycleConflict(
                    "correction observation cannot precede prior observation"
                )
            if prior_payload.get("provider_revision") == observation.provider_revision:
                raise OptionLifecycleConflict(
                    "correction requires a new provider revision"
                )
            if (
                prior_payload.get("provider_evidence_ref")
                == provider_evidence.evidence_ref
                or prior_payload.get("raw_evidence_digest")
                == observation.raw_evidence_digest
            ):
                raise OptionLifecycleConflict(
                    "correction requires fresh provider lifecycle evidence"
                )
            root_external_id = str(
                prior_payload.get("order_root_external_id")
                or prior_payload.get("external_event_id")
            )
            active_ids = tuple(prior_payload.get("active_transaction_ids", ()))
            by_id = {item.transaction_id: item for item in economic_cut.transactions}
            missing = [transaction_id for transaction_id in active_ids if transaction_id not in by_id]
            if missing:
                raise OptionLifecycleConflict(
                    "prior lifecycle economics are missing from canonical economic book"
                )
            old_active_transactions = tuple(by_id[item] for item in active_ids)
            if len(old_active_transactions) > 1:
                raise OptionLifecycleConflict(
                    "unsupported multi-transaction option lifecycle correction"
                )

        _require_consumable_option_position(
            economic_cut=economic_cut,
            observation=observation,
            version=version,
            old_active_transactions=old_active_transactions,
        )
        _require_physical_delivery_borrow_safety(
            economic_cut=economic_cut,
            observation=observation,
            version=version,
            old_active_transactions=old_active_transactions,
        )

        lifecycle_event_id = _identity(
            "option-lifecycle-event",
            observation.provider_id,
            observation.account_id,
            observation.environment,
            observation.external_event_id,
        )

        reversal_transactions: list[JournalTransaction] = []
        replacement_target: str | None = None
        if old_active_transactions:
            original = old_active_transactions[0]
            reversal_transactions.append(
                reverse_transaction(
                    original,
                    transaction_id=_identity(
                        "option-lifecycle-reversal",
                        observation.provider_id,
                        observation.account_id,
                        observation.environment,
                        observation.external_event_id,
                        original.transaction_id,
                    ),
                    cause_event_id=_identity(
                        "option-lifecycle-reversal-cause",
                        lifecycle_event_id,
                        original.transaction_id,
                    ),
                    observed_at=_utc_text(observation.observed_at),
                )
            )
            replacement_target = original.transaction_id

        replacement_cause_event_id = (
            lifecycle_event_id
            if replacement_target is None
            else _identity(
                "option-lifecycle-replacement-cause",
                lifecycle_event_id,
                replacement_target,
            )
        )
        replacement = _economic_transaction(
            observation,
            version,
            cause_event_id=replacement_cause_event_id,
            order_root_external_id=root_external_id,
            corrects_transaction_id=replacement_target,
        )
        economic_transactions = tuple(reversal_transactions + [replacement])

        try:
            plan = (
                DurableProviderEconomicBook.prepare_batch_mutation(
                    self.economic_book,
                    economic_transactions,
                    committed_at=_utc_text(observation.observed_at),
                    expected_previous_book_digest=economic_cut.book_digest,
                )
                if economic_transactions
                else None
            )
        except AccountingConflict as error:
            DurableProviderEconomicBook.refresh(self.economic_book)
            if str(error) == "economic book changed after validated read cut":
                raise OptionLifecycleConflict(
                    "canonical economic book changed after lifecycle position validation"
                ) from error
            raise
        if plan is not None and plan.already_committed:
            raise OptionLifecycleConflict(
                "fresh lifecycle identity maps to economics already committed elsewhere"
            )

        next_version = 1 if not events else int(events[-1]["aggregate_version"]) + 1
        active_transaction_ids = (replacement.transaction_id,)
        reversal_transaction_ids = tuple(
            item.transaction_id for item in reversal_transactions
        )
        lifecycle_payload = {
            "schema_version": "1.0.0",
            "provider_id": observation.provider_id,
            "account_id": observation.account_id,
            "environment": observation.environment,
            "provider_environment": observation.provider_environment,
            "venue_id": observation.venue_id,
            "instrument_version": observation.instrument_version,
            "instrument_digest": instrument_digest,
            "external_event_id": observation.external_event_id,
            "event_kind": observation.event_kind,
            "effective_at": _utc_text(observation.effective_at),
            "observed_at": _utc_text(observation.observed_at),
            "provider_revision": observation.provider_revision,
            "raw_evidence_digest": observation.raw_evidence_digest,
            "provider_evidence_ref": (
                qualified_provider_evidence.evidence_ref
                if qualified_provider_evidence is not None
                else provider_evidence.evidence_ref
            ),
            "provider_evidence_digest": provider_evidence_digest,
            "provider_evidence": provider_evidence_payload,
            "observation_digest": observation_digest,
            "corrects_external_event_id": observation.corrects_external_event_id,
            "order_root_external_id": root_external_id,
            "active_transaction_ids": list(active_transaction_ids),
            "reversal_transaction_ids": list(reversal_transaction_ids),
            "economic_batch_digest": plan.batch_digest if plan is not None else None,
        }
        lifecycle_envelope = {
            "event_id": lifecycle_event_id,
            "event_type": "OptionLifecycleApplied",
            "aggregate_type": self._AGGREGATE_TYPE,
            "aggregate_id": self.aggregate_id,
            "aggregate_version": str(next_version),
            "committed_at": _utc_text(observation.observed_at),
            "payload": lifecycle_payload,
            "payload_hash": payload_digest(lifecycle_payload),
        }

        request = {
            "schema_version": "1.0.0",
            "observation": observation_payload,
            "observation_digest": observation_digest,
            "provider_evidence": provider_evidence_payload,
            "provider_evidence_digest": provider_evidence_digest,
            "instrument_digest": instrument_digest,
        }
        result = {
            "lifecycle_event_id": lifecycle_event_id,
            "active_transaction_ids": list(active_transaction_ids),
            "reversal_transaction_ids": list(reversal_transaction_ids),
            "corrected_external_event_id": observation.corrects_external_event_id,
        }
        commit_events: list[tuple[dict[str, Any], str | None]] = [
            (lifecycle_envelope, "autotrade.option.lifecycle")
        ]
        if plan is not None:
            if plan.envelope is None:
                raise OptionLifecycleConflict("fresh economic plan has no durable event")
            commit_events.append((plan.envelope, "autotrade.economic.events"))

        command_id = _identity(