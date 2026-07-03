CREATE TABLE [Purchasing].[PurchaseOrderHeader] (
    [PurchaseOrderID] int NOT NULL,
    [RevisionNumber] tinyint NOT NULL,
    [Status] tinyint NOT NULL,
    [EmployeeID] int NOT NULL,
    [VendorID] int NOT NULL,
    [ShipMethodID] int NOT NULL,
    [OrderDate] datetime NOT NULL,
    [ShipDate] datetime NULL,
    [SubTotal] money NOT NULL,
    [TaxAmt] money NOT NULL,
    [Freight] money NOT NULL,
    [TotalDue] money NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
