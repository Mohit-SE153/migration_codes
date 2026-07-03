CREATE TABLE [Sales].[SalesOrderDetail] (
    [SalesOrderID] int NOT NULL,
    [SalesOrderDetailID] int NOT NULL,
    [CarrierTrackingNumber] nvarchar(25) NULL,
    [OrderQty] smallint NOT NULL,
    [ProductID] int NOT NULL,
    [SpecialOfferID] int NOT NULL,
    [UnitPrice] money NOT NULL,
    [UnitPriceDiscount] money NOT NULL,
    [LineTotal] numeric(38,6) NOT NULL,
    [rowguid] uniqueidentifier NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
